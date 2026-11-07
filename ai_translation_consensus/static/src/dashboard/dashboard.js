import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { formatInteger } from "@web/views/fields/formatters";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

const { DateTime } = luxon;

const REFRESH_MS = 8000;

export class AiTranslationDashboard extends Component {
    static template = "ai_translation_consensus.Dashboard";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            data: null,
            updated: null,
            auto: true,
            loading: false,
            busy: {},
            open: {},
            error: false,
        });
        onWillStart(() => this.load());
        this.timer = setInterval(() => {
            if (this.state.auto && !document.hidden) {
                this.load();
            }
        }, REFRESH_MS);
        onWillUnmount(() => clearInterval(this.timer));
    }

    async load() {
        if (this.state.loading) {
            return;
        }
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("ai.translation.watch", "get_dashboard", []);
            this.state.updated = new Date();
            this.state.error = false;
        } catch (error) {
            this.state.error = true;
        } finally {
            this.state.loading = false;
        }
    }

    get updatedText() {
        return this.state.updated ? DateTime.fromJSDate(this.state.updated).toLocaleString(DateTime.TIME_WITH_SECONDS) : "";
    }

    num(value) {
        return formatInteger(value || 0);
    }

    pct(part, total) {
        return total ? Math.round((100 * part) / total) : 0;
    }

    width(part, total) {
        return total ? `${(100 * part) / total}%` : "0%";
    }

    percentDone(rule) {
        return this.pct(rule.closed, rule.total);
    }

    kindLabel(rule) {
        return rule.kind === "translate" ? _t("Translation") : _t("Proofreading");
    }

    doneTitle(rule) {
        return rule.kind === "translate" ? _t("All languages translated") : _t("All texts proofread");
    }

    toggleOpen(rule) {
        this.state.open[rule.id] = !this.state.open[rule.id];
    }

    forecastCost(rule) {
        if (!rule.queued) {
            return "—";
        }
        if (!rule.forecast.ready) {
            return _t("known after the first batch");
        }
        if (rule.unpriced.length && !rule.forecast.cost) {
            return _t("enter prices on the providers");
        }
        return "≈ " + rule.forecast.cost_text;
    }

    forecastTime(rule) {
        if (!rule.queued) {
            return "—";
        }
        if (!rule.forecast.ready) {
            return _t("known after the first batch");
        }
        const own = "≈ " + rule.format.remaining;
        if (this.state.data.cron.active && rule.auto_scan) {
            return own + " " + _t("(with the schedule: %s)", rule.format.scheduled);
        }
        return own;
    }

    costNow(rule) {
        if (!rule.providers.length) {
            return "—";
        }
        return rule.cost_known ? rule.cost_text : _t("no prices entered");
    }

    async run(key, method, args, done) {
        if (this.state.busy[key]) {
            return;
        }
        this.state.busy[key] = true;
        try {
            const result = await this.orm.call("ai.translation.watch", method, args);
            this.notification.add(done(result), { type: "success" });
        } catch (error) {
            this.notification.add(error.data?.message || _t("The action failed."), { type: "danger" });
        } finally {
            this.state.busy[key] = false;
            await this.load();
        }
    }

    scan(rule) {
        return this.run(`scan${rule.id}`, "dashboard_scan", [[rule.id]], (n) =>
            _t("%s new texts queued.", n)
        );
    }

    process(rule) {
        return this.run(`proc${rule.id}`, "dashboard_process", [[rule.id]], (n) =>
            n ? _t("%s texts processed.", n) : _t("Nothing to process. Search for texts first.")
        );
    }

    retryFailed(rule) {
        return this.run(`retry${rule.id}`, "dashboard_retry_failed", [[rule.id]], (n) =>
            _t("%s failed texts queued again.", n)
        );
    }

    toggleCron() {
        return this.run("cron", "dashboard_toggle_cron", [], (active) =>
            active ? _t("Scheduled processing is on.") : _t("Scheduled processing is off.")
        );
    }

    openUnits(rule, state) {
        const domain = [["watch_id", "=", rule.id]];
        if (state) {
            domain.push(["state", "=", state]);
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            name: rule.name,
            res_model: "ai.translation.unit",
            views: [[false, "list"], [false, "form"]],
            domain,
        });
    }

    openRule(rule) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "ai.translation.watch",
            res_id: rule.id,
            views: [[false, "form"]],
        });
    }
}

registry.category("actions").add("ai_translation_consensus.dashboard", AiTranslationDashboard);
