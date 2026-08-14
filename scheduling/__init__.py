"""Durable reminders, autonomous jobs, and request-time clock context.

``scheduling.scheduler`` owns job storage, ``scheduling.runner`` executes due
jobs and delivers their results, and ``scheduling.time_context`` builds the
clock block injected into every model request.
"""
