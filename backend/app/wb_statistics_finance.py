"""Read the shared financial realization ledger for Statistics."""


def project_statistics_operations(rows, start, end):
    from app.repricer_page_finance import project_realization_operations
    return project_realization_operations(rows, start, end)


def load_statistics_finance(org, start, end):
    from app.repricer_page_finance import load_page_finance
    return load_page_finance(org, start, end, projector=project_statistics_operations)
