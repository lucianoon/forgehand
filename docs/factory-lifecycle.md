# Local workspace lifecycle

The controller stores `control/lifecycle.sqlite3` outside the mounted checkout.
It inventories leases, transition events and container ownership tokens before
Docker creation. Tokens are never included in workflow status. The API exposes
the last 50 lifecycle transitions and current build phase; local paths are
restricted to operator-or-higher clients.

Each invocation holds a workflow-specific POSIX file lock. Local Git children
inherit the lock descriptor, preventing cleanup if a process survives the
controller. Cancellation kills the Git process group and awaits termination;
Docker cancellation awaits ownership-checked container removal. Uncertain
cleanup remains quarantined and cannot release the checkout.

Workers reconcile every ten seconds. Queued/processing jobs and locked
workspaces are left alone. Terminal or paused workflows use configured success
or failure retention. Released workspaces cannot be retried; create a new order.
The journal remains available after checkout deletion. Cache directories,
remote branches and PRs are not removed.

Restart reconstructs runtime metadata from the journal. Only a recorded
pre-execution partial checkout may be rebuilt, from its cached pinned SHA.
Ready/active work is never discarded to conceal a missing workspace.

For the MVP, use one controller host and a durable local filesystem. Locks and
the journal must share storage with workspaces if multiple processes are used;
cross-host ephemeral disks are not supported. Inspect quarantined resources
before manual intervention; never delete the control directory to bypass an
ownership or cleanup failure.

## Complete Git object cache

Caches are now full bare clones: a blobless partial clone cannot safely serve
as the source of an ordinary local checkout. The first live pilot exposed this
gap before any model call. Legacy promisor caches are materialized with a full
refetch before provisioning or reconstructing an incomplete checkout. The
regression test uses an actual filter-capable Git transport; local-path clones
ignore filtering and therefore did not reproduce the issue in earlier tests.

## Docker Desktop socket

Set `FACTORY_DOCKER_SOCKET` to the absolute local Docker socket when it is not
`/var/run/docker.sock`, for example `/Users/<user>/.docker/run/docker.sock` on
macOS. The API worker and the qualification runner must use the same daemon.
The controller never inherits a remote Docker host from repository code.
