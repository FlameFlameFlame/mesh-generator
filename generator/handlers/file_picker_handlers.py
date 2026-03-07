def pick_file(app_mod):
    """Open a native OS file picker and return the selected path."""
    import platform
    import subprocess

    from flask import jsonify

    def _is_user_cancel_message(msg: str) -> bool:
        return "User canceled" in msg or "(-128)" in msg

    def _pick_with_tk() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        path = filedialog.askdirectory(
            title="Open project directory (must contain config.yaml)",
            initialdir=app_mod.DEFAULT_OUTPUT_DIR,
        )
        root.destroy()
        return path or ""

    def _run_osascript(script: str, *, jxa: bool = False) -> subprocess.CompletedProcess:
        cmd = ["/usr/bin/osascript"]
        if jxa:
            cmd.extend(["-l", "JavaScript"])
        cmd.extend(["-e", script])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    def _pick_with_swift_open_panel() -> str:
        import tempfile
        from pathlib import Path

        swift_code = r'''
import AppKit

let panel = NSOpenPanel()
panel.canChooseFiles = false
panel.canChooseDirectories = true
panel.allowsMultipleSelection = false
panel.canCreateDirectories = false
panel.title = "Select project directory (must contain config.yaml)"
panel.prompt = "Open"
let response = panel.runModal()
if response == .OK, let url = panel.url {
    print(url.path)
    exit(0)
}
fputs("User canceled.\n", stderr)
exit(1)
'''
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".swift",
            prefix="mesh_open_panel_",
            delete=False,
        ) as f:
            f.write(swift_code)
            script_path = f.name
        try:
            result = subprocess.run(
                ["/usr/bin/swift", script_path],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                return (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            if _is_user_cancel_message(stderr):
                return ""
            raise RuntimeError(stderr or "Swift NSOpenPanel failed")
        finally:
            try:
                Path(script_path).unlink(missing_ok=True)
            except Exception:
                app_mod.logger.debug("Could not remove temporary Swift picker script", exc_info=True)

    try:
        if platform.system() == "Darwin":
            errors = []

            script_choose_file = (
                'set f to POSIX path of '
                '(choose file with prompt "Select project config.yaml")'
            )
            result_choose_file = _run_osascript(script_choose_file)
            if result_choose_file.returncode == 0:
                return jsonify({"path": (result_choose_file.stdout or "").strip()})
            stderr_choose_file = (result_choose_file.stderr or "").strip()
            if _is_user_cancel_message(stderr_choose_file):
                return jsonify({"path": ""})
            app_mod.logger.warning(
                "macOS AppleScript choose-file failed: rc=%s stderr=%s",
                result_choose_file.returncode,
                stderr_choose_file,
            )
            if stderr_choose_file:
                errors.append(f"choose-file: {stderr_choose_file}")

            script_choose_folder = (
                'set f to POSIX path of '
                '(choose folder with prompt "Select project directory (must contain config.yaml)")'
            )
            result_choose_folder = _run_osascript(script_choose_folder)
            if result_choose_folder.returncode == 0:
                return jsonify({"path": (result_choose_folder.stdout or "").strip()})
            stderr_choose_folder = (result_choose_folder.stderr or "").strip()
            if _is_user_cancel_message(stderr_choose_folder):
                return jsonify({"path": ""})
            app_mod.logger.warning(
                "macOS AppleScript choose-folder failed: rc=%s stderr=%s",
                result_choose_folder.returncode,
                stderr_choose_folder,
            )
            if stderr_choose_folder:
                errors.append(f"choose-folder: {stderr_choose_folder}")

            script_jxa = r'''
ObjC.import('AppKit');
const panel = $.NSOpenPanel.openPanel;
panel.setCanChooseFiles(false);
panel.setCanChooseDirectories(true);
panel.setAllowsMultipleSelection(false);
panel.setCanCreateDirectories(false);
panel.setTitle('Select project directory (must contain config.yaml)');
panel.setPrompt('Open');
const result = panel.runModal();
if (result == $.NSModalResponseOK) {
  $.puts(ObjC.unwrap(panel.URL.path));
  $.exit(0);
}
$.stderr.write('User canceled.');
$.exit(1);
'''
            result_jxa = _run_osascript(script_jxa, jxa=True)
            if result_jxa.returncode == 0:
                return jsonify({"path": (result_jxa.stdout or "").strip()})
            stderr_jxa = (result_jxa.stderr or "").strip()
            if _is_user_cancel_message(stderr_jxa):
                return jsonify({"path": ""})
            app_mod.logger.warning("macOS JXA picker failed: rc=%s stderr=%s", result_jxa.returncode, stderr_jxa)
            if stderr_jxa:
                errors.append(f"jxa: {stderr_jxa}")

            try:
                swift_path = _pick_with_swift_open_panel()
                return jsonify({"path": swift_path})
            except Exception:
                app_mod.logger.warning("Swift NSOpenPanel fallback failed on macOS", exc_info=True)

            try:
                fallback_path = _pick_with_tk()
                if fallback_path:
                    return jsonify({"path": fallback_path})
            except Exception:
                app_mod.logger.warning("Tk fallback picker failed on macOS", exc_info=True)
            detail = "; ".join(errors) or "unknown macOS picker failure"
            return jsonify({"error": f"Native picker failed: {detail}", "path": ""})
        return jsonify({"path": _pick_with_tk()})
    except Exception as e:
        app_mod.logger.warning("File picker failed: %s", e)
        return jsonify({"error": str(e), "path": ""})
