from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Vendor documents: copy the previously selected attachment into the uploaded file field."""
    cr.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'leaders_vendor_document' AND column_name = 'attachment_id'
    """)
    if not cr.fetchone():
        return
    cr.execute("SELECT id, attachment_id FROM leaders_vendor_document WHERE attachment_id IS NOT NULL")
    rows = cr.fetchall()
    env = api.Environment(cr, SUPERUSER_ID, {})
    for doc_id, attachment_id in rows:
        attachment = env["ir.attachment"].browse(attachment_id).exists()
        doc = env["leaders.vendor.document"].browse(doc_id)
        if attachment and attachment.datas and not doc.file:
            doc.write({"file": attachment.datas, "file_name": attachment.name})
