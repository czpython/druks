from druks.contrib.software_factory.models import Project, WorkItem


async def test_new_work_item_summary_needs_no_database_read(druks_db):
    project = await Project.create(name="Acme")
    item = await WorkItem.create(
        project=project,
        repo="acme/widget",
        title="Keep the recorded title",
        ticket_key="ACME-1",
    )
    druks_db.expunge_all()

    summary = item.get_summary()

    assert summary.key == "ACME-1"
    assert summary.title == "Keep the recorded title"
    assert summary.project_name == "Acme"
