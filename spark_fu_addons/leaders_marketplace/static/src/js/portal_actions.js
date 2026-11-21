/* Marketplace portal: confirmation + loading state of destructive buttons, inline validation.
 *
 * Dependency free and progressive: without this file every form still submits normally.
 *
 *   data-lm-confirm="Message"   on a <button>/<a> or a <form>: ask before acting (window.confirm);
 *   data-lm-confirm-ref="#id"   same, the message is the text of that element (translatable QWeb text)
 *   data-lm-loading             on a submit button: disable it and show a spinner once the form is
 *                               submitted, so a double click cannot submit twice
 *   form.lm-validate            show Bootstrap inline errors (.invalid-feedback) instead of the
 *                               browser bubble; server errors keep working without JS
 */
(function () {
    "use strict";

    // The message comes from data-lm-confirm, or from the text of the element named by
    // data-lm-confirm-ref="#id" (QWeb text is translated, custom data-* attributes are not).
    function confirmMessage(element) {
        if (!element || !element.getAttribute) {
            return null;
        }
        var direct = element.getAttribute("data-lm-confirm");
        if (direct) {
            return direct;
        }
        var ref = element.getAttribute("data-lm-confirm-ref");
        var holder = ref ? document.querySelector(ref) : null;
        return holder ? holder.textContent.trim() : null;
    }

    function submitterOf(event, form) {
        return event.submitter || form.querySelector("[type=submit][data-lm-loading], button:not([type])[data-lm-loading]");
    }

    function startLoading(button) {
        if (!button || button.classList.contains("lm-is-loading")) {
            return;
        }
        button.classList.add("lm-is-loading");
        button.setAttribute("aria-busy", "true");
        var spinner = document.createElement("span");
        spinner.className = "lm-spinner me-1";
        spinner.setAttribute("aria-hidden", "true");
        button.insertBefore(spinner, button.firstChild);
        // Disable after the browser has collected the submitter value.
        window.setTimeout(function () {
            button.disabled = true;
        }, 0);
    }

    // Native bubbles are replaced by inline messages only when this script runs.
    function enableInlineValidation() {
        document.querySelectorAll("form.lm-validate").forEach(function (form) {
            form.noValidate = true;
        });
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", enableInlineValidation);
    } else {
        enableInlineValidation();
    }

    // A refused save shows the page again with an error: the server hands back what was typed
    // (#lm_form_state) and it goes back into the form it came from, so nothing has to be typed twice.
    // Passwords and files are never kept (the browser cannot be given a file back).
    function restoreFormState() {
        var holder = document.getElementById("lm_form_state");
        if (!holder) {
            return;
        }
        var state;
        try {
            state = JSON.parse(holder.getAttribute("data-state") || "{}");
        } catch (error) {
            return;
        }
        var path = window.location.pathname;
        document.querySelectorAll("form").forEach(function (form) {
            if ((form.getAttribute("method") || "").toLowerCase() !== "post") {
                return;
            }
            var link = document.createElement("a");
            link.href = form.getAttribute("action") || path;
            if (link.pathname !== path) {
                return;  // another form of the page: it was not the one submitted
            }
            Object.keys(state).forEach(function (name) {
                var values = state[name];
                var fields = Array.prototype.filter.call(
                    form.querySelectorAll("[name]"), function (field) { return field.name === name; });
                fields.forEach(function (field, index) {
                    var type = (field.type || "").toLowerCase();
                    if (["hidden", "file", "password", "submit", "button"].indexOf(type) !== -1) {
                        return;
                    }
                    if (type === "checkbox" || type === "radio") {
                        field.checked = values.indexOf(field.value) !== -1;
                    } else if (field.tagName === "SELECT" && field.multiple) {
                        Array.prototype.forEach.call(field.options, function (option) {
                            option.selected = values.indexOf(option.value) !== -1;
                        });
                    } else {
                        field.value = values[fields.length > 1 ? index : 0] !== undefined
                            ? values[fields.length > 1 ? index : 0] : field.value;
                    }
                    field.dispatchEvent(new Event("input", {bubbles: true}));
                    field.dispatchEvent(new Event("change", {bubbles: true}));
                });
            });
        });
        var alertBox = document.getElementById("lm_form_error");
        if (alertBox && alertBox.scrollIntoView) {
            alertBox.scrollIntoView({block: "center"});
        }
    }
    // After "load": the other scripts of the page have drawn their parts (specs, variants) by then.
    window.addEventListener("load", restoreFormState);

    // Restore buttons when the page comes back from the bfcache (Back button).
    window.addEventListener("pageshow", function (event) {
        if (!event.persisted) {
            return;
        }
        document.querySelectorAll(".lm-is-loading").forEach(function (button) {
            button.classList.remove("lm-is-loading");
            button.removeAttribute("aria-busy");
            button.disabled = false;
            var spinner = button.querySelector(".lm-spinner");
            if (spinner) {
                spinner.remove();
            }
        });
    });

    document.addEventListener("click", function (event) {
        var target = event.target.closest ?
            event.target.closest("[data-lm-confirm], [data-lm-confirm-ref]") : null;
        if (!target || target.tagName === "FORM") {
            return;
        }
        var text = confirmMessage(target);
        if (text && !window.confirm(text)) {
            event.preventDefault();
            event.stopPropagation();
        }
    }, true);

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form || form.tagName !== "FORM") {
            return;
        }
        // Inline validation first: an invalid form is neither confirmed nor locked.
        if (form.classList.contains("lm-validate") && !form.checkValidity()) {
            event.preventDefault();
            event.stopPropagation();
            form.querySelectorAll(":invalid").forEach(function (field) {
                field.classList.add("lm-touched");
                if (field.classList.contains("form-control") || field.classList.contains("form-select")) {
                    field.setAttribute("aria-invalid", "true");
                }
            });
            var first = form.querySelector(":invalid");
            if (first) {
                first.focus();
            }
            return;
        }
        var button = submitterOf(event, form);
        // A confirmation on the submitter itself was already asked by the click handler.
        var message = confirmMessage(form);
        if (message && !window.confirm(message)) {
            event.preventDefault();
            return;
        }
        if (button && button.hasAttribute("data-lm-loading")) {
            startLoading(button);
        }
    });

    document.addEventListener("input", function (event) {
        var field = event.target;
        if (field.classList && field.classList.contains("lm-touched") && field.validity && field.validity.valid) {
            field.classList.remove("is-invalid");
        }
    });
    document.addEventListener("blur", function (event) {
        var field = event.target;
        if (field.form && field.form.classList && field.form.classList.contains("lm-validate") &&
                field.classList && (field.classList.contains("form-control") || field.classList.contains("form-select"))) {
            field.classList.add("lm-touched");
        }
    }, true);
})();
