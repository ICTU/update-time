# Registrations that survived

- 2026-09-04 `tests.update_time.sources.test_github.ChangesFromChangelogFileTest.test_directory_the_repository_does_not_serve` — a directory the contents endpoint answers with a file ends the run with a traceback
- 2026-09-05 `tests.update_time.updaters.test_update_requirements_txt.UpdateRequirementsTxtTest.test_archival_check_disabled` — the run-wide switch decides nothing, so an archived project is warned about with the check off
- 2026-09-05 `tests.update_time.updaters.test_update_requirements_txt.UpdateRequirementsTxtTest.test_archival_check_disabled` — the source is told not to check for archival but answers anyway, so an archived project is warned about with the check off
- 2026-09-08 `tests.update_time.file_formats.test_pyproject_toml.DeclaredDependenciesTest.test_a_declaration_on_the_first_line_reads_no_line_above_it` — the first line reads the last one as the line above it, so a marker ending a file steers the declaration starting it
