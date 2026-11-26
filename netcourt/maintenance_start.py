"""Provisional start decision from queued customer notice."""
from .notice_status import provisional_notified


def provisional_start(db, customers, revision):
    return all(provisional_notified(db, customer, revision) for customer in customers)
