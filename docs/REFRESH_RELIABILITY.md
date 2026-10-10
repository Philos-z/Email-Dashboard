# Background refresh reliability

SQLite connections close at the end of every database operation, including failed
operations. Successful transactions commit and failed transactions roll back before
closing. This prevents repeated background polling from exhausting the process's
file descriptor limit while waiting for garbage collection.

The hourly scheduler catches and logs temporary database or dispatch errors and
retries scheduling after five seconds. The item context worker also recovers from
temporary database errors. Cancellation still stops both workers during shutdown.
Mailbox refresh failures retain their existing hourly retry schedule and do not
advance sync checkpoints.

The mail status response includes:

| Field | Meaning |
| --- | --- |
| `schedulerHealthy` | The scheduler task is running without a current scheduling error |
| `schedulerLastTickAt` | UTC timestamp of the last successful scheduler check |
| `schedulerError` | A safe status message while scheduling is retrying |

An enabled schedule and a healthy scheduler are separate states. A successful tick
does not mean a mailbox refresh has succeeded; check the refresh run history too.

Outlook MCP calls retain their 60-second response timeout. Timeout messages identify
the stage, such as client startup, tool initialization, account verification, folder
listing, or message reading. Logs contain only operation names and elapsed time;
mail contents, folder IDs, and credentials are excluded.

Regression tests cover 1,200 database operations under a low file descriptor limit
with garbage collection disabled, commit and rollback behavior, scheduler recovery
after database and dispatch failures, context worker recovery, and safe timeout
diagnostics. The file descriptor limit test runs on POSIX systems.
