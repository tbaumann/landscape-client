import os
from collections import namedtuple

from twisted.internet.defer import Deferred

from landscape.manager.aptsources import AptSources
from landscape.manager.plugin import SUCCEEDED, FAILED

from landscape.lib.twisted_util import gather_results
from landscape.tests.helpers import LandscapeTest, ManagerHelper
from landscape.package.reporter import find_reporter_command


class AptSourcesTests(LandscapeTest):
    helpers = [ManagerHelper]

    def setUp(self):
        super(AptSourcesTests, self).setUp()
        self.sourceslist = AptSources()
        self.sources_path = self.makeDir()
        self.sourceslist.SOURCES_LIST = os.path.join(self.sources_path,
                                                     "sources.list")
        sources_d = os.path.join(self.sources_path, "sources.list.d")
        os.mkdir(sources_d)
        self.sourceslist.SOURCES_LIST_D = sources_d
        self.manager.add(self.sourceslist)

        sources = file(self.sourceslist.SOURCES_LIST, "w")
        sources.write("\n")
        sources.close()

        service = self.broker_service
        service.message_store.set_accepted_types(["operation-result"])

        self.sourceslist._run_process = lambda cmd, args, *aarg, **kargs: None

    def test_comment_sources_list(self):
        """
        When getting a repository message, L{AptSources} comments the whole
        sources.list file.
        """
        sources = file(self.sourceslist.SOURCES_LIST, "w")
        sources.write("oki\n\ndoki\n#comment\n # other comment\n")
        sources.close()

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [], "gpg-keys": [],
             "operation-id": 1})

        self.assertEqual(
            "#oki\n\n#doki\n#comment\n # other comment\n",
            file(self.sourceslist.SOURCES_LIST).read())

        service = self.broker_service
        self.assertMessages(service.message_store.get_pending_messages(),
                            [{"type": "operation-result",
                              "status": SUCCEEDED, "operation-id": 1}])

    def test_sources_list_permissions(self):
        """
        When getting a repository message, L{AptSources} keeps sources.list
        permissions.
        """
        sources = file(self.sourceslist.SOURCES_LIST, "w")
        sources.write("oki\n\ndoki\n#comment\n # other comment\n")
        sources.close()
        # change file mode from default to check it's restored
        os.chmod(self.sourceslist.SOURCES_LIST, 0400)
        sources_stat_orig = os.stat(self.sourceslist.SOURCES_LIST)

        FakeStatResult = namedtuple("FakeStatResult",
                                    ["st_mode", "st_uid", "st_gid"])
        fake_stats = FakeStatResult(st_mode=sources_stat_orig.st_mode,
                                    st_uid=30, st_gid=30)
        os_stat = self.mocker.replace("os.stat")
        os_stat(self.sourceslist.SOURCES_LIST)
        self.mocker.result(fake_stats)
        self.mocker.count(1, max=10)
        os_chown = self.mocker.replace("os.chown")
        os_chown(self.sourceslist.SOURCES_LIST,
                 fake_stats.st_uid, fake_stats.st_gid)
        self.mocker.replay()

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [], "gpg-keys": [],
             "operation-id": 1})

        service = self.broker_service
        self.assertMessages(service.message_store.get_pending_messages(),
                            [{"type": "operation-result",
                              "status": SUCCEEDED, "operation-id": 1}])

        sources_stat_after = os.stat(self.sourceslist.SOURCES_LIST)
        self.assertEqual(sources_stat_orig.st_mode, sources_stat_after.st_mode)

    def test_random_failures(self):
        """
        If a failure happens during the manipulation of sources, the activity
        is reported as FAILED with the error message.
        """
        self.sourceslist.SOURCES_LIST = "/doesntexist"

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [], "gpg-keys": [],
             "operation-id": 1})

        msg = "IOError: [Errno 2] No such file or directory: '/doesntexist'"
        service = self.broker_service
        self.assertMessages(service.message_store.get_pending_messages(),
                            [{"type": "operation-result",
                              "result-text": msg, "status": FAILED,
                              "operation-id": 1}])

    def test_rename_sources_list_d(self):
        """
        The sources files in sources.list.d are renamed to .save when a message
        is received.
        """
        sources1 = file(
            os.path.join(self.sourceslist.SOURCES_LIST_D, "file1.list"), "w")
        sources1.write("ok\n")
        sources1.close()

        sources2 = file(
            os.path.join(self.sourceslist.SOURCES_LIST_D,
                         "file2.list.save"), "w")
        sources2.write("ok\n")
        sources2.close()

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [], "gpg-keys": [],
             "operation-id": 1})

        self.assertFalse(
            os.path.exists(
                os.path.join(self.sourceslist.SOURCES_LIST_D, "file1.list")))

        self.assertTrue(
            os.path.exists(
                os.path.join(self.sourceslist.SOURCES_LIST_D,
                             "file1.list.save")))

        self.assertTrue(
            os.path.exists(
                os.path.join(self.sourceslist.SOURCES_LIST_D,
                             "file2.list.save")))

    def test_create_landscape_sources(self):
        """
        For every sources listed in the sources field of the message,
        C{AptSources} creates a file with the content in sources.list.d.
        """
        sources = [{"name": "dev", "content": "oki\n"},
                   {"name": "lucid", "content": "doki\n"}]
        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": sources, "gpg-keys": [],
             "operation-id": 1})

        dev_file = os.path.join(self.sourceslist.SOURCES_LIST_D,
                                "landscape-dev.list")
        self.assertTrue(os.path.exists(dev_file))
        self.assertEqual("oki\n", file(dev_file).read())

        lucid_file = os.path.join(self.sourceslist.SOURCES_LIST_D,
                                  "landscape-lucid.list")
        self.assertTrue(os.path.exists(lucid_file))
        self.assertEqual("doki\n", file(lucid_file).read())

    def test_import_gpg_keys(self):
        """
        C{AptSources} runs a process with apt-key for every keys in the
        message.
        """
        deferred = Deferred()

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            self.assertEqual("/usr/bin/apt-key", command)
            self.assertEqual("add", args[0])
            filename = args[1]
            self.assertEqual("Some key content", file(filename).read())
            deferred.callback(("ok", "", 0))
            return deferred

        self.sourceslist._run_process = _run_process

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [],
             "gpg-keys": ["Some key content"], "operation-id": 1})

        return deferred

    def test_import_delete_temporary_files(self):
        """
        The files created to be imported by C{apt-key} are removed after the
        import.
        """
        deferred = Deferred()
        filenames = []

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            if not filenames:
                filenames.append(args[1])
                deferred.callback(("ok", "", 0))
                return deferred

        self.sourceslist._run_process = _run_process

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [],
             "gpg-keys": ["Some key content"], "operation-id": 1})

        self.assertFalse(os.path.exists(filenames[0]))

        return deferred

    def test_failed_import_delete_temporary_files(self):
        """
        The files created to be imported by C{apt-key} are removed after the
        import, even if there is a failure.
        """
        deferred = Deferred()
        filenames = []

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            filenames.append(args[1])
            deferred.callback(("error", "", 1))
            return deferred

        self.sourceslist._run_process = _run_process

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [],
             "gpg-keys": ["Some key content"], "operation-id": 1})

        self.assertFalse(os.path.exists(filenames[0]))

        return deferred

    def test_failed_import_reported(self):
        """
        If the C{apt-key} command failed for some reasons, the output of the
        command is reported and the activity fails.
        """
        deferred = Deferred()

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            deferred.callback(("nok", "some error", 1))
            return deferred

        self.sourceslist._run_process = _run_process

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [], "gpg-keys": ["key"],
             "operation-id": 1})

        service = self.broker_service
        msg = "ProcessError: nok\nsome error"
        self.assertMessages(service.message_store.get_pending_messages(),
                            [{"type": "operation-result",
                              "result-text": msg, "status": FAILED,
                              "operation-id": 1}])
        return deferred

    def test_signaled_import_reported(self):
        """
        If the C{apt-key} fails with a signal, the output of the command is
        reported and the activity fails.
        """
        deferred = Deferred()

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            deferred.errback(("nok", "some error", 1))
            return deferred

        self.sourceslist._run_process = _run_process

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [], "gpg-keys": ["key"],
             "operation-id": 1})

        service = self.broker_service
        msg = "ProcessError: nok\nsome error"
        self.assertMessages(service.message_store.get_pending_messages(),
                            [{"type": "operation-result",
                              "result-text": msg, "status": FAILED,
                              "operation-id": 1}])
        return deferred

    def test_failed_import_no_changes(self):
        """
        If the C{apt-key} command failed for some reasons, the current
        repositories aren't changed.
        """
        deferred = Deferred()

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            deferred.callback(("nok", "some error", 1))
            return deferred

        self.sourceslist._run_process = _run_process

        sources = file(self.sourceslist.SOURCES_LIST, "w")
        sources.write("oki\n\ndoki\n#comment\n")
        sources.close()

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [], "gpg-keys": ["key"],
             "operation-id": 1})

        self.assertEqual(
            "oki\n\ndoki\n#comment\n",
            file(self.sourceslist.SOURCES_LIST).read())

        return deferred

    def test_multiple_import_sequential(self):
        """
        If multiple keys are specified, the imports run sequentially, not in
        parallel.
        """
        deferred1 = Deferred()
        deferred2 = Deferred()
        deferreds = [deferred1, deferred2]

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            if not deferreds:
                return None
            return deferreds.pop(0)

        self.sourceslist._run_process = _run_process

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [],
             "gpg-keys": ["key1", "key2"], "operation-id": 1})

        self.assertEqual(1, len(deferreds))
        deferred1.callback(("ok", "", 0))

        self.assertEqual(0, len(deferreds))
        deferred2.callback(("ok", "", 0))

        service = self.broker_service
        self.assertMessages(service.message_store.get_pending_messages(),
                            [{"type": "operation-result",
                              "status": SUCCEEDED, "operation-id": 1}])
        return gather_results(deferreds)

    def test_multiple_import_failure(self):
        """
        If multiple keys are specified, and that the first one fails, the error
        is correctly reported.
        """
        deferred1 = Deferred()
        deferred2 = Deferred()
        deferreds = [deferred1, deferred2]

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            return deferreds.pop(0)

        self.sourceslist._run_process = _run_process

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [],
             "gpg-keys": ["key1", "key2"], "operation-id": 1})

        deferred1.callback(("error", "", 1))
        deferred2.callback(("error", "", 1))

        msg = "ProcessError: error\n"
        service = self.broker_service
        self.assertMessages(service.message_store.get_pending_messages(),
                            [{"type": "operation-result",
                              "result-text": msg, "status": FAILED,
                              "operation-id": 1}])
        return gather_results(deferreds)

    def test_run_reporter(self):
        """
        After receiving a message, L{AptSources} triggers a reporter run to
        have the new packages reported to the server.
        """
        deferred = Deferred()

        def _run_process(command, args, env={}, path=None, uid=None, gid=None):
            self.assertEqual(find_reporter_command(), command)
            self.assertEqual(["--force-smart-update", "--config=%s" %
                              self.manager.config.config], args)
            deferred.callback(("ok", "", 0))
            return deferred

        self.sourceslist._run_process = _run_process

        self.manager.dispatch_message(
            {"type": "apt-sources-replace", "sources": [], "gpg-keys": [],
             "operation-id": 1})

        return deferred