from odoo import _, models
from odoo.exceptions import AccessError


class LeadersBackofficeMixin(models.AbstractModel):
    """Guard for actions only the marketplace back-office may run.

    ACLs stop a portal vendor from *writing* these records, but the action
    methods stay public and reachable through ``/web/dataset/call_kw``. This
    mixin is the second lock on that door.
    """

    _name = "leaders.backoffice.mixin"
    _description = "Marketplace Back-Office Guard"

    # Which marketplace group owns which kind of decision.
    _LEADERS_BACKOFFICE_ROLES = {
        "reviewer": "leaders_marketplace.group_marketplace_reviewer",
        "finance": "leaders_marketplace.group_marketplace_finance",
        "manager": "leaders_marketplace.group_marketplace_manager",
    }
    _LEADERS_MANAGER_GROUP = "leaders_marketplace.group_marketplace_manager"

    def _leaders_check_backoffice(self, role="manager"):
        """Raise AccessError unless the caller may run a platform-only action.

        Passes for the superuser -- every internal flow (portal routes, stock
        and fulfillment sync, crons) reaches these methods through ``sudo()``
        -- and for members of the group that owns the action. The marketplace
        manager group implies both reviewer and finance, so it always passes.

        :param role: ``reviewer`` (content decisions), ``finance`` (money) or
            ``manager`` (marketplace operations).
        """
        if self.env.su:
            return True
        group = self._LEADERS_BACKOFFICE_ROLES[role]
        if self.env.user.has_groups("%s,%s" % (group, self._LEADERS_MANAGER_GROUP)):
            return True
        raise AccessError(_(
            "Only the marketplace back-office can run this action on %(model)s.",
            model=self._description or self._name,
        ))
