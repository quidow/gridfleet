const releaseManagedScopes = ["backend", "agent", "frontend", "testkit"];
const releasePleaseTypes = ["deps", "feat", "fix", "perf"];

export default {
  extends: ["@commitlint/config-conventional"],
  plugins: [
    {
      rules: {
        "release-please-type": (parsed) => {
          if (!releaseManagedScopes.includes(parsed.scope)) {
            return [true];
          }

          const isBreaking = parsed.header.startsWith(`${parsed.type}(${parsed.scope})!:`)
            || parsed.notes.some((note) => note.title === "BREAKING CHANGE");

          if (isBreaking || releasePleaseTypes.includes(parsed.type)) {
            return [true];
          }

          return [
            false,
            `release-managed scope "${parsed.scope}" must use a release-please type (${releasePleaseTypes.join(
              ", ",
            )}) or breaking-change marker`,
          ];
        },
      },
    },
  ],
  rules: {
    "release-please-type": [2, "always"],
    "type-enum": [
      2,
      "always",
      ["build", "chore", "ci", "deps", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"],
    ],
    "scope-empty": [2, "never"],
    "scope-enum": [
      2,
      "always",
      ["backend", "agent", "frontend", "testkit", "docker", "ci", "docs", "deps", "deps-dev", "main"],
    ],
    "subject-min-length": [2, "always", 10],
    "subject-case": [2, "always", "lower-case"],
  },
};
