def migrate(cr, version):
    cr.execute("UPDATE spark_blog_comment SET state = 'draft' WHERE state = 'pending'")
