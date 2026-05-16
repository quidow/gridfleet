export default {
  extends: ["@commitlint/config-conventional"],
  rules: {
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
    "subject-case": [2, "never", ["sentence-case", "start-case", "pascal-case", "upper-case"]],
  },
};
