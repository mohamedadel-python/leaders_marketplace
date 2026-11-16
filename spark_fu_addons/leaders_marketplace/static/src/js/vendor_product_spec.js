/* Vendor product form: show only the catalog fields that apply.
 *
 * The category option carries its field set (data-field-set: "equipment" or "part"); the
 * equipment block shows for equipment, the spare-part block for parts, and the used-equipment
 * block only when the condition is used or refurbished. Without this file every block shows.
 *
 * Also: "Other" in the brand and model lists opens a text box; the models follow the chosen
 * brand; a used item is one piece and is inspected before it is published; the rent price
 * shows only for a product offered for rent.
 */
(function () {
    "use strict";

    function toggle(el, visible) {
        if (el) {
            el.classList.toggle("d-none", !visible);
        }
    }

    function filterModels(form) {
        var brand = form.querySelector("#lm_sp_brand");
        var model = form.querySelector("#lm_sp_model");
        if (!brand || !model) {
            return;
        }
        var chosen = brand.value && brand.value !== "__other__" ? brand.value : "";
        Array.prototype.forEach.call(model.options, function (option) {
            var of = option.getAttribute("data-brand");
            if (of === null) {
                return; // the placeholder and "Other"
            }
            var visible = !chosen || of === chosen;
            option.hidden = !visible;
            option.disabled = !visible;
        });
        var selected = model.options[model.selectedIndex];
        if (selected && selected.disabled && selected.value) {
            model.value = "";
        }
    }

    function update(form) {
        var category = form.querySelector("#lm_pf_categ");
        var condition = form.querySelector("#lm_sp_condition");
        var option = category && category.options[category.selectedIndex];
        var fieldSet = option ? option.getAttribute("data-field-set") || "" : "";
        var used = condition && (condition.value === "used" || condition.value === "refurbished");
        form.querySelectorAll("[data-lm-spec]").forEach(function (block) {
            var kind = block.getAttribute("data-lm-spec");
            var visible = kind === "used" ? used : kind === fieldSet;
            block.classList.toggle("d-none", !visible);
        });
        toggle(form.querySelector("#lm_used_notice"), used);

        var brand = form.querySelector("#lm_sp_brand");
        toggle(form.querySelector("#lm_sp_brand_other"), brand && brand.value === "__other__");
        var model = form.querySelector("#lm_sp_model");
        toggle(form.querySelector("#lm_sp_model_other"), model && model.value === "__other__");

        var listing = form.querySelector("#lm_sp_listing");
        toggle(form.querySelector("#lm_sp_rent_wrap"), listing && listing.value === "rent");

        // a used item is one piece
        var qty = form.querySelector("#lm_pf_qty");
        if (qty) {
            if (used) {
                qty.value = "1";
            }
            qty.readOnly = !!used;
            toggle(form.querySelector("#lm_pf_qty_used"), used);
        }
    }

    function init() {
        var form = document.getElementById("lm_product_form");
        if (!form || !form.querySelector("#lm_spec_card")) {
            return;
        }
        form.addEventListener("change", function (event) {
            var id = event.target && event.target.id;
            if (id === "lm_sp_brand") {
                filterModels(form);
            }
            if (["lm_pf_categ", "lm_sp_condition", "lm_sp_brand", "lm_sp_model", "lm_sp_listing"].indexOf(id) !== -1) {
                update(form);
            }
        });
        filterModels(form);
        update(form);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
