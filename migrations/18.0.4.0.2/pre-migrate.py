def migrate(cr, version):
    """Map the old vendor document types to the new supported list."""
    cr.execute("""
        UPDATE leaders_vendor_document
           SET document_type = CASE document_type
                   WHEN 'national_id' THEN 'id_passport'
                   WHEN 'lease' THEN 'address_proof'
                   WHEN 'other' THEN 'address_proof'
                   ELSE document_type END
         WHERE document_type IN ('national_id', 'lease', 'other')
    """)
