#!/usr/bin/env python3
"""Exercise extracted adaptive synchronization code without Kodi/network dependencies."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = Path(os.environ.get(
    "PS5_ADDON_BUILD",
    ROOT / "tools/depends/target/binary-addons/x86_64-unknown-freebsd-debug"))
SOURCE = WRAPPER / "build/inputstream.adaptive/src/common"
BUILD = ROOT / "build/ps5-adaptive-worker-tests"
BUILD.mkdir(parents=True, exist_ok=True)

def test_patch_application():
    patch = ROOT / "tools/ps5/patches/inputstream-adaptive-ps5.patch"
    script = ROOT / "tools/ps5/apply-adaptive-patch.cmake"
    names = ("src/utils/log.h", "src/Stream.cpp",
             "src/common/AdaptiveStream.cpp", "src/common/AdaptiveStream.h")
    with tempfile.TemporaryDirectory(prefix="patch-", dir=BUILD) as directory:
        tree = Path(directory)
        for name in names:
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE.parents[1] / name, target)
        subprocess.run(["patch", "--batch", "--force", "--reverse", "-p1"],
                       cwd=tree, input=patch.read_text(), text=True, check=True,
                       stdout=subprocess.DEVNULL)
        original_header = (tree / names[-1]).read_bytes()
        command = ["cmake", f"-DSOURCE_DIR={tree}", "-P", str(script)]
        integration_source = (ROOT / "cmake/addons/CMakeLists.txt").read_text()
        start = integration_source.index(
            '          if(CORE_PLATFORM_NAME STREQUAL ps5 AND id STREQUAL "inputstream.adaptive")')
        end = integration_source.index("          # create a list of addons", start)
        integration = tree / "integration.cmake"
        integration.write_text(integration_source[start:end])
        integration_command = [
            "cmake", f"-DCORE_SOURCE_DIR={ROOT}", f"-DSOURCE_DIR={tree}"]
        for platform, addon in (("linux", "inputstream.adaptive"), ("ps5", "test.binary.ps5")):
            subprocess.run(integration_command + [
                f"-DCORE_PLATFORM_NAME={platform}", f"-Did={addon}",
                "-P", str(integration)], check=True)
            if (tree / names[-1]).read_bytes() != original_header:
                raise RuntimeError("PS5 adaptive integration changed another platform/add-on")
        subprocess.run(integration_command + [
            "-DCORE_PLATFORM_NAME=ps5", "-Did=inputstream.adaptive",
            "-P", str(integration)], check=True)
        subprocess.run(command, check=True)
        for name in names:
            if (tree / name).read_bytes() != (SOURCE.parents[1] / name).read_bytes():
                raise RuntimeError(f"Patch did not reproduce accepted source: {name}")
        (tree / names[-1]).write_bytes(original_header)
        before = {name: (tree / name).read_bytes() for name in names}
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 or "partially patched" not in result.stderr:
            raise RuntimeError("Mixed patch state was not rejected explicitly")
        if any((tree / name).read_bytes() != before[name] for name in names):
            raise RuntimeError("Rejected patch modified source")
    print("PASS: PS5-only integration, clean patch application, idempotence and conflict rejection")


test_patch_application()


def block(text, signature):
    start = text.index(signature)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


header = (SOURCE / "AdaptiveStream.h").read_text()
source = (SOURCE / "AdaptiveStream.cpp").read_text()
segments = (SOURCE / "SegmentBuffer.cpp").read_text()
thread = block(header, "struct THREADDATA") + ";"
initialize = block(source, "void adaptive::AdaptiveStream::THREADDATA::Initialize")
stop = block(source, "void adaptive::AdaptiveStream::THREADDATA::StopDownloads")
wait = block(segments, "void ADP::CSegmentBuffers::WaitForSegment")
reset = block(segments, "void ADP::CSegmentBuffers::Reset")
dispose = block(source, "void adaptive::AdaptiveStream::Dispose")
dispose = dispose.replace("  segment_read_pos_ = 0;\n", "").replace(
    "  absolute_position_ = 0;\n", "")
worker = source[source.index("void adaptive::AdaptiveStream::worker()"):
                source.index("      DownloadInfo downloadInfo;", source.index(
                    "void adaptive::AdaptiveStream::worker()"))]
worker = worker.replace("  do\n", "  while (!start) std::this_thread::yield();\n  do\n", 1)
worker = worker.replace("      thread_data_->cvState.wait", "      phase = 1;\n      thread_data_->cvState.wait")
worker = worker.replace("    m_segBuffers.WaitForSegment();",
                        "    phase = 2;\n    m_segBuffers.WaitForSegment();")
worker = worker.replace(
    "      std::lock_guard<std::mutex> lckWorker",
    "      if (pauseBeforeDownload) {\n"
    "        phase = 3;\n"
    "        while (!resumeDownload) std::this_thread::yield();\n"
    "      }\n      std::lock_guard<std::mutex> lckWorker")
worker += """
      ++bufferAccesses;
    }
  } while (!thread_data_->IsThreadExit());
  finished = true;
}
"""

# The waits, exit publisher/destructor, and pre-download checks come from the
# patched source. Only downloads and Kodi's log sink are replaced.
fixture = """
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <thread>
#include <vector>
namespace LOG { inline void PS5StopPhase(const char*, const void*) {} }
namespace ADP {
class CSegmentBuffers {
public:
  void WaitForSegment();
  void Reset();
private:
  std::mutex m_mutex;
  std::condition_variable m_cvWaitSegment;
  std::vector<int> m_buffers;
  size_t m_downloadIndex{0};
  bool m_forceUnlock{false};
};
}
namespace adaptive {
class AdaptiveStream {
public:
""" + thread + """
  void worker();
  void Dispose();
  THREADDATA* thread_data_{nullptr};
  ADP::CSegmentBuffers m_segBuffers;
  std::atomic<int> phase{0};
  std::atomic<bool> start{false}, finished{false};
  bool pauseBeforeDownload{false};
  std::atomic<bool> resumeDownload{false};
  int bufferAccesses{0};
};
}
using adaptive::AdaptiveStream;
""" + "\n".join((initialize, stop, wait, reset, worker, dispose))
(BUILD / "adaptive-worker-fixture.h").write_text(fixture)
compiler = shlex.split(os.environ.get("CXX", "clang++-18"))
subprocess.run(compiler + [
    "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
    "-I", str(BUILD), str(ROOT / "tools/ps5/tests/adaptive-worker-shutdown.cpp"),
    "-o", str(BUILD / "adaptive-worker-shutdown")], check=True)
subprocess.run([str(BUILD / "adaptive-worker-shutdown")], check=True, timeout=30)
