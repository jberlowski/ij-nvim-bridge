Small independent Java/JUnit Gradle build for the parent-workspace acceptance
suite (`tests/test_workspace_projects.py`). The harness copies it into A and B,
changes A's identity, and supplies the existing fixture's Gradle wrapper. Both
builds deliberately contain the same fully qualified classes and task names.
Open their parent in IntelliJ and link both builds; no root aggregator build.
