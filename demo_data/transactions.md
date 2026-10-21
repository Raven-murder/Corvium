# Transactions: keeping related changes together

A database transaction groups operations into one logical unit of work. Consider a transfer of 200 units from account A to account B. The application must reduce A's balance and increase B's balance together. If it commits only the debit, money disappears from the recorded system. If it commits only the credit, money appears. A transaction defines the boundary within which both changes must succeed or both must be undone.

## The ACID properties

Atomicity means that a transaction's changes take effect together or do not take effect. A rollback abandons its uncommitted work. Consistency means that a correct transaction takes the database from one valid state to another, respecting declared constraints and application rules. For this transfer, the combined balance should remain unchanged, assuming no fee. Database constraints help enforce rules, but the database cannot repair arbitrary mistakes in business logic.

Isolation controls how concurrent transactions interact. The aim is to prevent interference inconsistent with the selected isolation guarantees. Durability means that once a commit succeeds, its effects survive the failures covered by the database's storage and recovery design. Durability is distinct from backups: surviving a process crash does not necessarily protect against destruction of the entire storage device.

## A transfer sequence

Begin a transaction, verify that both account identifiers exist, and check that A has sufficient available funds. Debit A and credit B, then commit. If a required check fails or either update raises an error, roll back. The balance check and debit must be protected against concurrent transfers. Reading a balance in one transaction and updating it later without coordination can allow two requests to spend the same funds.

One approach is an update that subtracts the amount only when the current balance is sufficient, followed by checking the affected row count. Another approach uses suitable row locking. The exact syntax and behavior depend on the database. Keep transactions short and avoid waiting for human input while holding locks, because blocked work can accumulate behind them.

## Concurrency problems and isolation

A dirty read observes another transaction's uncommitted changes, which might later disappear. A nonrepeatable read occurs when rereading a row returns a different committed value. A phantom occurs when repeating a predicate query finds a changed set of matching rows. A lost update happens when one writer overwrites another writer's change without incorporating it.

Isolation levels offer different guarantees, and implementation details vary across products. Read committed prevents dirty reads but can permit results to change between statements. Serializable aims to make committed concurrent transactions equivalent to some serial execution. Stronger isolation may involve waiting or aborting conflicting work; applications must handle those outcomes rather than assuming every request succeeds immediately.

## Recovery and safe retries

A deadlock can occur when two transactions each hold a resource the other needs. Databases commonly break the cycle by aborting a participant. Retry the entire transaction with bounded attempts, rather than replaying only its final statement. For payment-like requests, use a unique request identifier to avoid applying the same logical operation twice after an uncertain network response. A timeout does not prove that a commit failed. Review exercises should distinguish rollback, retry, and duplicate-request handling: they solve different failure cases.
