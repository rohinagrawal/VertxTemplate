---
description: Maven build guidance for VertxTemplate
alwaysApply: true
---

# Maven Build Rules

VertxTemplate is a Java 21 Vert.x Maven project.

## Defaults

- Use `mvn -q -DskipTests compile` for a quick compile check.
- Use `mvn test` when tests are explicitly requested.
- Use `mvn -q -DskipTests package` to produce the shaded jar.
- Use `mvn -q -DskipTests exec:java` to run the main verticle through the configured Exec plugin.

## Validation Notes

- Prefer commands from the repo root so Maven picks up the project `pom.xml` and its Vert.x/Java 21 settings.
- Do not add build workarounds for dependency or compiler issues until the root-level Maven command has been validated.
- If you introduce repo-local Maven settings later, document them here and keep them free of secrets.

## Useful Commands

```bash
mvn -q -DskipTests compile
mvn test
mvn -q -DskipTests package
mvn -q -DskipTests exec:java
```

