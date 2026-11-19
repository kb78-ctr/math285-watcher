# MATH 285 seat watcher

Checks UIUC Course Explorer every ~5 minutes for an open seat in the 9:00 AM
section of MATH 285 (Spring 2027) and sends a phone alert via ntfy.

- Alerts once per opening (state is kept in `state.json`).
- Stop it: Actions tab -> "Check MATH 285 9AM seat" -> ... -> Disable workflow.
- Run a check now: Actions tab -> the workflow -> Run workflow.
