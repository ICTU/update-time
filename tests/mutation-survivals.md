# Registrations that survived

- 2026-09-04 `tests.update_time.sources.test_github.ChangesFromChangelogFileTest.test_directory_the_repository_does_not_serve` — a directory the contents endpoint answers with a file ends the run with a traceback
- 2026-09-05 `tests.update_time.updaters.test_update_requirements_txt.UpdateRequirementsTxtTest.test_archival_check_disabled` — the run-wide switch decides nothing, so an archived project is warned about with the check off
- 2026-09-05 `tests.update_time.updaters.test_update_requirements_txt.UpdateRequirementsTxtTest.test_archival_check_disabled` — the source is told not to check for archival but answers anyway, so an archived project is warned about with the check off
- 2026-09-08 `tests.update_time.file_formats.test_pyproject_toml.DeclaredDependenciesTest.test_a_declaration_on_the_first_line_reads_no_line_above_it` — the first line reads the last one as the line above it, so a marker ending a file steers the declaration starting it
- 2026-09-13 `tests.update_time.io.test_log.RecordRenderingTests.test_the_markdown_parser_logs_nothing_at_debug_level` — the Markdown parser traces every block rule it tries, burying the run's own debug output
- 2026-09-13 `tests.update_time.io.test_log.RecordRenderingTests.test_a_links_url_is_printed_where_nothing_can_be_clicked` — a raw HTML block is dropped, taking the changes a `<details>` section wraps with it
- 2026-09-13 `tests.update_time.io.test_log.RecordRenderingTests.test_a_record_without_changes_gets_no_empty_block` — a comment the changelog's author wrote to be invisible is printed in the log
- 2026-09-13 `tests.update_time.io.test_log.RecordRenderingTests.test_a_links_url_is_printed_where_nothing_can_be_clicked` — every record without changes gets a blank line below it
- 2026-09-13 `tests.update_time.io.test_log.RecordRenderingTests.test_an_html_comment_is_not_shown` — every record without changes gets a blank line below it
- 2026-09-13 `tests.update_time.io.test_log.RecordRenderingTests.test_changes_that_are_not_markdown_render_as_written` — every record without changes gets a blank line below it
- 2026-09-13 `tests.update_time.io.test_log.RecordRenderingTests.test_raw_html_in_a_markdown_changelog_is_shown_as_written` — every record without changes gets a blank line below it
- 2026-09-13 `tests.update_time.io.test_log.RecordRenderingTests.test_the_markdown_parser_logs_nothing_at_debug_level` — every record without changes gets a blank line below it
