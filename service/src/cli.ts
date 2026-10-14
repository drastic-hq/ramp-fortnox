import { Effect } from "effect";
import { readFile } from "node:fs/promises";
import { config, evidence, Gate, requireThat } from "./domain";
import { Store } from "./store";
import { Connector, task } from "./connector";

const args = process.argv.slice(2),
  command = args[0];
if (args.includes("--help") || command === "help" || !command) {
  console.log(
    JSON.stringify({
      state: "help",
      commands: [
        "connect --config <file>",
        "inspect --transaction <id> --evidence <file> --config <file>",
        "plan --transaction <id> --evidence <file> --config <file>",
        "apply --operation <id> --fingerprint <sha256> --config <file>",
        "reconcile --operation <id> --fingerprint <sha256> --config <file>",
        "status --operation <id> [--proof <directory>] --config <file>",
        "worker --once --operation <id> --fingerprint <sha256> --config <file>",
      ],
      note: "Sandbox grants, calibrated receipt evidence and observed readback probes gate accounting writes. Synthetic mode requires CONNECTOR_TEST_MODE=loopback-only.",
    }),
  );
  process.exit(0);
}
function option(name: string, required = true): string | undefined {
  const index = args.indexOf("--" + name);
  const value = index < 0 ? undefined : args[index + 1];
  if (required)
    requireThat(value && !value.startsWith("--"), "missing-" + name);
  return value;
}
const program = Effect.gen(function* () {
  const c = yield* task(async () =>
    config(JSON.parse(await readFile(option("config")!, "utf8"))),
  );
  return yield* Effect.acquireUseRelease(
    task(async () => new Store(c)),
    (store) =>
      Effect.gen(function* () {
        yield* task(() => store.migrate());
        const connector = new Connector(c, store);
        if (command === "connect") return yield* connector.connect();
        if (command === "inspect" || command === "plan") {
          const e = yield* task(async () =>
            evidence(JSON.parse(await readFile(option("evidence")!, "utf8"))),
          );
          return yield* command === "plan"
            ? connector.plan(option("transaction")!, e)
            : connector.inspect(option("transaction")!, e);
        }
        if (command === "status")
          return yield* connector.status(
            option("operation")!,
            option("proof", false),
          );
        if (
          command === "apply" ||
          command === "reconcile" ||
          command === "worker"
        ) {
          if (command === "worker")
            requireThat(args.includes("--once"), "worker-once-required");
          return yield* connector.apply(
            option("operation")!,
            option("fingerprint")!,
            command === "reconcile",
          );
        }
        return yield* Effect.fail(new Gate("unknown-command"));
      }),
    (store) => Effect.promise(() => store.close()),
  );
});
const result = await Effect.runPromise(Effect.result(program));
if (result._tag === "Failure") {
  console.log(
    JSON.stringify({ state: "blocked", reason: result.failure.message }),
  );
  process.exitCode = 2;
} else {
  console.log(JSON.stringify(result.success));
  const state = result.success.state;
  process.exitCode =
    state === "uncertain"
      ? 3
      : state === "investigate"
        ? 4
        : state === "blocked"
          ? 2
          : 0;
}
