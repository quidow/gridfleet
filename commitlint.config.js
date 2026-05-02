export default {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "scope-enum": [
      2,
      "always",
      ["backend", "agent", "frontend", "testkit", "docker", "ci", "docs", "deps"],
    ],
    "subject-min-length": [2, "always", 10],
    "subject-case": [2, "always", "lower-case"],
  },
};
