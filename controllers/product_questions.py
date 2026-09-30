"""Customer side of the product questions (TASK 29 / D3).

Behind the switch ``leaders_marketplace.product_questions_enabled`` (default off: 404).
The vendor side lives in controllers/portal.py (``/my/vendor/questions``).
"""
from urllib.parse import quote

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request
from odoo.addons.leaders_marketplace.models.product_question import clean_text


class MarketplaceProductQuestions(http.Controller):

    @http.route("/shop/product/<int:template_id>/question", type="http", auth="public",
                website=True, methods=["POST"], csrf=True)
    def ask_question(self, template_id, question=None, product_id=None, **kw):
        Question = request.env["leaders.product.question"]
        if not Question._leaders_enabled():
            raise NotFound()
        template = request.env["product.template"].sudo().browse(template_id).exists()
        if not template or not template.sale_ok or not template.is_published \
                or not template.can_access_from_current_website():
            raise NotFound()
        page = template.website_url
        user = request.env.user
        if user._is_public():
            return request.redirect("/web/login?redirect=%s" % quote(page))
        product = request.env["product.product"].sudo().browse(int(product_id)) \
            if product_id and str(product_id).isdigit() else request.env["product.product"]
        code = Question._leaders_ask_problem(template, user, clean_text(question)) or "ok"
        if code == "ok":
            with request.env.cr.savepoint():
                Question._leaders_ask(template, user, question, product=product.exists())
        return request.redirect("%s?lm_q=%s#lm_questions" % (page, code))
