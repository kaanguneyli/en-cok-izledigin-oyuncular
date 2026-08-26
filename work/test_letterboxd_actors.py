import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "outputs" / "letterboxd_actors.py"
SPEC = importlib.util.spec_from_file_location("letterboxd_actors", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ListingParserTests(unittest.TestCase):
    def test_films_are_extracted_and_later_deduplicated(self):
        parser = MODULE.ListingParser("films")
        parser.feed(
            """
            <a class="frame" href="/film/arrival/" data-original-title="Arrival (2016)"></a>
            <a class="frame" href="/film/arrival/" data-original-title="Arrival (2016)"></a>
            <a class="next" href="/kaan/films/page/2/">Older</a>
            """
        )
        self.assertEqual([film.slug for film in parser.entries], ["arrival", "arrival"])
        self.assertEqual(parser.next_href, "/kaan/films/page/2/")

    def test_films_are_extracted_from_server_rendered_posters(self):
        parser = MODULE.ListingParser("films")
        parser.feed(
            """
            <div class="react-component" data-component-class="LazyPoster"
              data-item-link="/film/arrival/"
              data-item-full-display-name="Arrival (2016)"></div>
            """
        )
        self.assertEqual(parser.entries, [MODULE.Film("arrival", "Arrival (2016)")])

    def test_diary_keeps_repeated_logs(self):
        parser = MODULE.ListingParser("diary")
        parser.feed(
            """
            <h1><span title="2&nbsp;films">Diary</span></h1>
            <table>
              <tr class="diary-entry-row"><td><div data-item-link="/film/arrival/"
                data-item-full-display-name="Arrival (2016)"></div></td></tr>
              <tr class="diary-entry-row"><td><div data-item-link="/film/arrival/"
                data-item-full-display-name="Arrival (2016)"></div></td></tr>
            </table>
            """
        )
        self.assertEqual([film.slug for film in parser.entries], ["arrival", "arrival"])
        self.assertEqual(parser.reported_total, 2)

    def test_profile_listings_are_read_in_parallel(self):
        barrier = threading.Barrier(2)
        collections = {
            "films": MODULE.ModeCollection(),
            "diary": MODULE.ModeCollection(),
        }

        def collect(_client, _username, mode, _quiet, _max_pages, _collection=None):
            barrier.wait(timeout=2)
            return collections[mode]

        with patch.object(MODULE, "collect_listing", side_effect=collect):
            result = MODULE.collect_profile_listings(object(), "kaan", True)

        self.assertIs(result["films"], collections["films"])
        self.assertIs(result["diary"], collections["diary"])

    def test_complete_diary_skips_remaining_films_pages(self):
        arrival = MODULE.Film("arrival", "Arrival (2016)")
        enemy = MODULE.Film("enemy", "Enemy (2013)")
        films_first_page = MODULE.ModeCollection(
            films={arrival.slug: arrival},
            weights=MODULE.Counter({arrival.slug: 1}),
            reported_total=2,
            next_url="https://letterboxd.com/kaan/films/page/2/",
            pages_read=1,
        )
        diary_first_page = MODULE.ModeCollection(
            films={arrival.slug: arrival},
            weights=MODULE.Counter({arrival.slug: 2}),
            reported_total=3,
            next_url="https://letterboxd.com/kaan/diary/page/2/",
            pages_read=1,
        )
        diary_complete = MODULE.ModeCollection(
            films={arrival.slug: arrival, enemy.slug: enemy},
            weights=MODULE.Counter({arrival.slug: 2, enemy.slug: 1}),
            reported_total=3,
            pages_read=2,
        )
        continued_modes = []

        def collect(_client, _username, mode, _quiet, _max_pages=None, collection=None):
            if collection is None:
                return films_first_page if mode == "films" else diary_first_page
            continued_modes.append(mode)
            return diary_complete

        with patch.object(MODULE, "collect_listing", side_effect=collect):
            result = MODULE.collect_profile_listings(object(), "kaan", True)

        self.assertEqual(continued_modes, ["diary"])
        self.assertEqual(set(result["films"].films), {"arrival", "enemy"})
        self.assertEqual(result["films"].weights, {"arrival": 1, "enemy": 1})


class CastAndRankingTests(unittest.TestCase):
    def test_cast_limit_preserves_billing_order_and_supports_all(self):
        cast = [
            MODULE.Actor("lead", "Lead"),
            MODULE.Actor("support", "Support"),
            MODULE.Actor("cameo", "Cameo"),
        ]
        casts = {"film": cast}

        self.assertEqual(
            [actor.slug for actor in MODULE.limit_casts(casts, 2)["film"]],
            ["lead", "support"],
        )
        self.assertIs(MODULE.limit_casts(casts, 0), casts)

    def test_cast_panel_wins_and_combined_ranking_counts_rewatches(self):
        parser = MODULE.CastParser()
        parser.feed(
            """
            <a href="/actor/not-cast/">Not Cast</a>
            <div id="tab-panel-cast"><div class="cast-list">
              <a href="/actor/amy-adams/">Amy Adams</a>
              <a href="/actor/jeremy-renner/">Jeremy Renner</a>
              <a href="/actor/amy-adams/">Amy Adams</a>
            </div></div>
            """
        )
        self.assertEqual([actor.slug for actor in parser.actors], ["amy-adams", "jeremy-renner"])

        arrival = MODULE.Film("arrival", "Arrival (2016)")
        enchanted = MODULE.Film("enchanted", "Enchanted (2007)")
        films_collection = MODULE.ModeCollection(
            films={arrival.slug: arrival, enchanted.slug: enchanted},
            weights=MODULE.Counter({arrival.slug: 1, enchanted.slug: 1}),
        )
        diary_collection = MODULE.ModeCollection(
            films={arrival.slug: arrival}, weights=MODULE.Counter({arrival.slug: 3})
        )
        rankings = MODULE.rank_combined_actors(
            films_collection,
            diary_collection,
            {
                arrival.slug: parser.actors,
                enchanted.slug: [MODULE.Actor("amy-adams", "Amy Adams")],
            },
        )
        self.assertEqual(rankings[0].appearances, 4)
        self.assertEqual(len(rankings[0].films), 2)

    def test_workbook_payload_includes_rewatches_and_film_names(self):
        actor = MODULE.Actor("amy-adams", "Amy Adams")
        total = MODULE.ActorTotal(
            actor=actor,
            appearances=3,
            films={"arrival": ("Arrival (2016)", 3)},
        )
        row = MODULE.rankings_payload([total], None)[0]
        self.assertEqual(row["actor"], "Amy Adams")
        self.assertEqual(row["appearances"], 3)
        self.assertEqual(row["uniqueFilms"], 1)
        self.assertEqual(row["rewatches"], 2)
        self.assertEqual(row["films"], "Arrival (2016) x3")
        self.assertEqual(
            row["filmEntries"],
            [{"slug": "arrival", "title": "Arrival (2016)", "views": 3}],
        )

    def test_film_catalog_orders_by_views_then_actor_count(self):
        films = {
            "arrival": MODULE.Film("arrival", "Arrival (2016)"),
            "enemy": MODULE.Film("enemy", "Enemy (2013)"),
            "dune": MODULE.Film("dune", "Dune (2021)"),
        }
        weights = {"arrival": 2, "enemy": 2, "dune": 1}
        casts = {
            "arrival": [MODULE.Actor("amy-adams", "Amy Adams")],
            "enemy": [
                MODULE.Actor("jake-gyllenhaal", "Jake Gyllenhaal"),
                MODULE.Actor("melanie-laurent", "Melanie Laurent"),
            ],
            "dune": [
                MODULE.Actor("timothee-chalamet", "Timothee Chalamet"),
                MODULE.Actor("zendaya", "Zendaya"),
                MODULE.Actor("rebecca-ferguson", "Rebecca Ferguson"),
            ],
        }

        catalog = MODULE.film_catalog_payload(films, weights, casts)

        self.assertEqual([film["slug"] for film in catalog], ["enemy", "arrival", "dune"])

    def test_ui_export_respects_excluded_films_query_and_sort(self):
        result = {
            "username": "kaan",
            "films": [
                {"slug": "arrival", "views": 2},
                {"slug": "enemy", "views": 1},
                {"slug": "dune", "views": 1},
            ],
            "rows": [
                {
                    "actor": "Amy Adams",
                    "actorUrl": "https://letterboxd.com/actor/amy-adams/",
                    "filmEntries": [
                        {"slug": "arrival", "title": "Arrival", "views": 2},
                        {"slug": "enemy", "title": "Enemy", "views": 1},
                    ],
                },
                {
                    "actor": "Jake Gyllenhaal",
                    "actorUrl": "https://letterboxd.com/actor/jake-gyllenhaal/",
                    "filmEntries": [
                        {"slug": "arrival", "title": "Arrival", "views": 2}
                    ],
                },
                {
                    "actor": "Rebecca Ferguson",
                    "actorUrl": "https://letterboxd.com/actor/rebecca-ferguson/",
                    "filmEntries": [
                        {"slug": "dune", "title": "Dune", "views": 1}
                    ],
                },
            ],
            "errors": [],
        }

        payload = MODULE.ui_export_payload(
            result,
            {
                "excludedFilms": ["arrival"],
                "query": "",
                "sortKey": "appearances",
                "sortDirection": "desc",
            },
        )

        self.assertEqual(payload["summary"], {"totalViews": 2, "uniqueFilms": 2, "rewatches": 0})
        self.assertEqual(
            payload["sortDescription"], "Sıralama izlenme sayısına göre azalan."
        )
        self.assertEqual([row["actor"] for row in payload["rows"]], ["Amy Adams", "Rebecca Ferguson"])
        self.assertEqual([row["rank"] for row in payload["rows"]], [1, 2])
        self.assertEqual(payload["rows"][0]["films"], "Enemy")

        searched = MODULE.ui_export_payload(result, {"query": "dune"})
        self.assertEqual([row["actor"] for row in searched["rows"]], ["Rebecca Ferguson"])


class UsernameTests(unittest.TestCase):
    def test_accepts_username_or_profile_url(self):
        self.assertEqual(MODULE.normalize_username("kaan_1"), "kaan_1")
        self.assertEqual(
            MODULE.normalize_username("https://letterboxd.com/kaan_1/"), "kaan_1"
        )


class UICommandTests(unittest.TestCase):
    def test_ui_command_reuses_cli_without_reopening_ui(self):
        output_dir = Path("~/Desktop/letterboxd-output")
        command = MODULE.build_ui_command(
            "https://letterboxd.com/kaan_1/", output_dir, refresh=True
        )

        self.assertEqual(command[1], "-u")
        self.assertEqual(command[3], "kaan_1")
        self.assertIn(str(output_dir.expanduser().resolve()), command)
        self.assertIn("--display", command)
        self.assertIn("--cast-limit", command)
        self.assertEqual(command[command.index("--cast-limit") + 1], "20")
        self.assertIn("--refresh", command)
        self.assertIn("--ui-result", command)
        self.assertNotIn("--ui", command)

    def test_account_is_optional_only_for_ui_entry_point(self):
        args = MODULE.build_argument_parser().parse_args([])
        self.assertIsNone(args.account)

    def test_ui_document_contains_controls_and_escapes_script_content(self):
        document = MODULE.ui_document("</script>").decode("utf-8")

        self.assertIn("<title>En Çok İzlediğin Oyuncular</title>", document)
        self.assertIn("<h1>En Çok İzlediğin Oyuncular</h1>", document)
        self.assertIn("Letterboxd hesabı", document)
        self.assertIn('id="castLimit"', document)
        self.assertIn('castLimit: Number(castLimit.value)', document)
        self.assertNotIn("Letterboxd Oyuncu Analizi", document)
        self.assertNotIn("brand-mark", document)
        self.assertIn("Analiz et", document)
        self.assertIn("Oyuncu veya film ara", document)
        self.assertIn('data-sort="appearances"', document)
        self.assertIn('data-sort="uniqueFilms"', document)
        self.assertIn('data-sort="rewatches"', document)
        self.assertIn('fullList.textContent = remaining.join("; ")', document)
        self.assertIn('id="filmFilterDialog"', document)
        self.assertIn('aria-label="Filmleri filtrele"', document)
        self.assertIn('class="filter-label">Filtrele</span>', document)
        self.assertIn('<option value="20" selected>20</option>', document)
        self.assertIn('<option value="200">200</option>', document)
        self.assertIn('<option value="all">Hepsi</option>', document)
        self.assertIn('id="previousPage"', document)
        self.assertIn('id="nextPage"', document)
        self.assertIn('id="activityPanel" class="activity-panel hidden"', document)
        self.assertIn('activityPanel.classList.add("hidden")', document)
        self.assertIn('id="exportExcel"', document)
        self.assertIn("Excel indir", document)
        self.assertIn('/api/export', document)
        self.assertIn('sessionStorage.getItem("letterboxdJobId")', document)
        self.assertIn('/api/status?job=', document)
        self.assertIn('jobId: currentJobId', document)
        self.assertIn("function applySelectedFilms()", document)
        self.assertNotIn('id="more"', document)
        self.assertNotIn("visibleLimit", document)
        self.assertNotIn("/api/download", document)
        self.assertIn("\\u003c/script>", document)
        self.assertNotIn("const initialAccount = \"</script>\"", document)

    def test_hosted_ui_hides_local_shutdown_control(self):
        document = MODULE.ui_document("kaan", hosted=True).decode("utf-8")

        self.assertIn('id="shutdown" class="quiet-dark hidden"', document)
        self.assertNotIn("__SHUTDOWN_CLASS__", document)


class UIJobRegistryTests(unittest.TestCase):
    class FakeManager:
        instances = []

        def __init__(self, _cache_dir, job_id="", on_finish=None):
            self.job_id = job_id
            self.on_finish = on_finish
            self.running = False
            self.touched = MODULE.time.monotonic()
            self.__class__.instances.append(self)

        def start(self, account, _refresh, cast_limit=20):
            self.account = account
            self.cast_limit = cast_limit
            self.running = True
            return self.snapshot()

        def snapshot(self):
            self.touched = MODULE.time.monotonic()
            return {
                "jobId": self.job_id,
                "status": "running" if self.running else "success",
                "label": "Çalışıyor" if self.running else "Tamamlandı",
                "log": "",
                "result": None,
            }

        def is_running(self):
            return self.running

        def retention_state(self):
            return self.running, self.touched

        def cancel(self):
            self.running = False
            if self.on_finish:
                self.on_finish(self.job_id)
            return self.snapshot()

        def export_workbook(self, _options):
            return "actors.xlsx", self.job_id.encode("ascii")

    def setUp(self):
        self.FakeManager.instances = []

    def test_registry_isolates_jobs_and_limits_concurrent_analysis(self):
        with patch.object(MODULE, "UIJobManager", self.FakeManager):
            registry = MODULE.UIJobRegistry(Path("/tmp"))
            first = registry.start("kaan", False)
            with self.assertRaisesRegex(ValueError, "başka bir analizi"):
                registry.start("another", False)

            registry.cancel(first["jobId"])
            second = registry.start("another", False, 30)

            self.assertNotEqual(first["jobId"], second["jobId"])
            self.assertEqual(self.FakeManager.instances[-1].cast_limit, 30)
            self.assertEqual(registry.snapshot(first["jobId"])["jobId"], first["jobId"])
            self.assertEqual(
                registry.export_workbook(second["jobId"], {})[1],
                second["jobId"].encode("ascii"),
            )

    def test_registry_rejects_invalid_cast_limit(self):
        registry = MODULE.UIJobRegistry(Path("/tmp"))

        with self.assertRaisesRegex(ValueError, "1 ile 100"):
            registry.start("kaan", False, 0)

    def test_registry_rejects_unknown_job_ids(self):
        registry = MODULE.UIJobRegistry(Path("/tmp"))

        with self.assertRaisesRegex(ValueError, "İş bulunamadı"):
            registry.snapshot("missing")


class OutputModeTests(unittest.TestCase):
    def collections_and_casts(self):
        film = MODULE.Film("arrival", "Arrival (2016)")
        films = MODULE.ModeCollection(
            films={film.slug: film}, weights=MODULE.Counter({film.slug: 1})
        )
        diary = MODULE.ModeCollection(
            films={film.slug: film}, weights=MODULE.Counter({film.slug: 2})
        )
        casts = {film.slug: [MODULE.Actor("amy-adams", "Amy Adams")]}
        return films, diary, casts

    def run_with_mode(self, ui_result):
        films, diary, casts = self.collections_and_casts()
        arguments = ["kaan", "--display", "0", "--output-dir", "/tmp"]
        if ui_result:
            arguments.append("--ui-result")
        args = MODULE.build_argument_parser().parse_args(arguments)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(
            MODULE,
            "collect_profile_listings",
            return_value={"films": films, "diary": diary},
        ), patch.object(
            MODULE, "collect_casts", return_value=(casts, [])
        ), patch.object(MODULE, "write_workbook") as write_workbook, redirect_stdout(
            stdout
        ), redirect_stderr(
            stderr
        ):
            return_code = MODULE.run(args)
        return return_code, stdout.getvalue(), write_workbook

    def test_ui_result_mode_returns_data_without_writing_excel(self):
        return_code, stdout, write_workbook = self.run_with_mode(ui_result=True)

        self.assertEqual(return_code, 0)
        write_workbook.assert_not_called()
        result_line = next(
            line for line in stdout.splitlines() if line.startswith(MODULE.UI_RESULT_PREFIX)
        )
        payload = json.loads(result_line[len(MODULE.UI_RESULT_PREFIX) :])
        self.assertEqual(payload["summary"]["totalViews"], 2)
        self.assertEqual(payload["rows"][0]["actor"], "Amy Adams")
        self.assertEqual(payload["films"][0]["slug"], "arrival")
        self.assertEqual(payload["films"][0]["actorCount"], 1)

    def test_cli_mode_still_writes_excel(self):
        return_code, stdout, write_workbook = self.run_with_mode(ui_result=False)

        self.assertEqual(return_code, 0)
        write_workbook.assert_called_once()
        self.assertIn("Oluşturulan dosya:", stdout)

    @unittest.skipUnless(
        importlib.util.find_spec("xlsxwriter"), "XlsxWriter is not installed"
    )
    def test_portable_workbook_is_valid_xlsx(self):
        payload = {
            "username": "kaan",
            "summary": {"totalViews": 2, "uniqueFilms": 1, "rewatches": 1},
            "rows": [
                {
                    "rank": 1,
                    "actor": "Amy Adams",
                    "appearances": 2,
                    "uniqueFilms": 1,
                    "rewatches": 1,
                    "actorUrl": "https://letterboxd.com/actor/amy-adams/",
                    "films": "Arrival (2016) x2",
                }
            ],
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "actors.xlsx"
            MODULE.write_portable_workbook(output, payload)

            with zipfile.ZipFile(output) as archive:
                self.assertIn("xl/workbook.xml", archive.namelist())


if __name__ == "__main__":
    unittest.main()
