/** @odoo-module **/

import { loadBundle } from "@web/core/assets";
import { registry } from "@web/core/registry";
import { getColor } from "@web/core/colors/colors";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onWillStart, useEffect, useRef } from "@odoo/owl";
import { cookie } from "@web/core/browser/cookie";

export class CommissionGraphField extends Component {
    static template = "leaders_marketplace.CommissionGraphField";
    static props = { ...standardFieldProps };

    setup() {
        this.chart = null;
        this.canvasRef = useRef("canvas");
        onWillStart(async () => await loadBundle("web.chartjs_lib"));
        useEffect(() => {
            this.renderChart();
            return () => this.chart && this.chart.destroy();
        });
    }

    renderChart() {
        if (this.chart) {
            this.chart.destroy();
        }
        const data = JSON.parse(this.props.record.data[this.props.name] || "{}");
        const color = getColor(3, cookie.get("color_scheme"), "odoo");
        this.chart = new Chart(this.canvasRef.el, {
            type: "line",
            data: {
                datasets: [{
                    data: data.values || [],
                    fill: "start",
                    borderColor: color,
                    backgroundColor: "rgba(113, 75, 103, 0.18)",
                    borderWidth: 2,
                    pointRadius: 3,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false }, tooltip: { intersect: false } },
                scales: {
                    x: { type: "linear", ticks: { callback: value => `${value}%` } },
                    y: { ticks: { callback: value => `${value} ${data.currency || ""}` } },
                },
                elements: { line: { tension: 0.000001 } },
            },
        });
    }
}

registry.category("fields").add("commission_graph", {
    component: CommissionGraphField,
    supportedTypes: ["text"],
});
