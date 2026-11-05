import Foundation
import Darwin

// This executable is compiled only into a separate signed test app. Its input
// paths contain disposable files created by native_sandbox_smoke.py.
let info = Bundle.main.infoDictionary!
let runID = info["ProbeRunID"] as! String
let mode = info["ProbeMode"] as! String
let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Agent Board")
let resultURL = support.appendingPathComponent("sandbox-proof-" + runID + ".json")
let runningURL = support.appendingPathComponent("sandbox-running-" + runID + ".json")
var report: [String: Any] = ["mode": mode, "run_id": runID, "checks": [String: Bool](), "support": support.path]
let manager = CollectorRuntime()

func launchArtifacts() -> Set<String> {
    let names = (try? FileManager.default.contentsOfDirectory(atPath: support.appendingPathComponent("data").path)) ?? []
    return Set(names.filter { $0.hasPrefix(".collector-access-") || $0.hasPrefix(".source-bookmarks-") })
}
let initialLaunchArtifacts = launchArtifacts()
report["preexisting_launch_artifacts"] = Array(initialLaunchArtifacts).sorted()

func record(_ name: String, _ pass: Bool) {
    var checks = report["checks"] as! [String: Bool]
    checks[name] = pass
    report["checks"] = checks
}

func save(_ destination: URL, _ value: [String: Any]) {
    try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
    try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .prettyPrinted]).write(to: destination, options: .atomic)
}

func finish(_ failure: String? = nil) {
    manager.stop()
    if let failure { report["error"] = failure }
    report["passed"] = failure == nil && (report["checks"] as! [String: Bool]).values.allSatisfy { $0 }
    save(resultURL, report)
    exit(report["passed"] as? Bool == true ? 0 : 1)
}

func request(_ url: URL, token: String?, body: [String: Any]? = nil, csrf: String? = nil) -> (Int, [String: Any]) {
    var request = URLRequest(url: url)
    request.timeoutInterval = 3
    if let token { request.setValue(token, forHTTPHeaderField: "X-Agent-Board-Access") }
    if let body {
        request.httpMethod = "POST"
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(csrf, forHTTPHeaderField: "X-Agent-Board-Token")
        request.setValue("http://127.0.0.1:\(url.port!)", forHTTPHeaderField: "Origin")
    }
    let semaphore = DispatchSemaphore(value: 0)
    var result: (Int, [String: Any]) = (0, [:])
    URLSession.shared.dataTask(with: request) { data, response, _ in
        result = ((response as? HTTPURLResponse)?.statusCode ?? 0,
                  data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:])
        semaphore.signal()
    }.resume()
    if semaphore.wait(timeout: .now() + 5) != .success { return (0, [:]) }
    return result
}

record("sandbox_mode_selected", manager.sandboxed)
record("storage_inside_app_container", support.path.contains("/Library/Containers/local.agentboard.sandbox-proof-harness/Data/"))
let outside = URL(fileURLWithPath: info["ProbeOutsideFile"] as! String)
record("outside_source_read_denied", (try? Data(contentsOf: outside)) == nil)
do {
    try Data("unexpected".utf8).write(to: outside)
    record("outside_source_write_denied", false)
} catch { record("outside_source_write_denied", true) }
let external = Process()
external.executableURL = URL(fileURLWithPath: info["ProbeOutsideExecutable"] as! String)
external.standardOutput = FileHandle.nullDevice
external.standardError = FileHandle.nullDevice
do {
    try external.run()
    external.waitUntilExit()
    record("outside_executable_denied", false)
} catch { record("outside_executable_denied", true) }

// Verify the immutable sandbox policy blocks credential modes even when the
// parent environment does not contain the normal sandbox flag.
let policy = manager.executable.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Resources/sandbox-collector-policy.json")
let policyValue = (try? Data(contentsOf: policy)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
guard policyValue?["sandbox_required"] as? Bool == true else { finish("The packaged sandbox policy is missing."); exit(1) }
let credentialProbe = Process()
credentialProbe.executableURL = manager.executable
credentialProbe.arguments = ["--authorize-claude"]
var environment = ProcessInfo.processInfo.environment
environment.removeValue(forKey: "AGENT_BOARD_SANDBOX")
environment.removeValue(forKey: "AGENT_BOARD_SOURCE_BOOKMARKS")
credentialProbe.environment = environment
let credentialOutput = Pipe()
credentialProbe.standardOutput = credentialOutput
credentialProbe.standardError = credentialOutput
do {
    try credentialProbe.run()
    let output = credentialOutput.fileHandleForReading.readDataToEndOfFile()
    credentialProbe.waitUntilExit()
    let text = String(data: output, encoding: .utf8) ?? ""
    record("immutable_policy_blocks_credential_mode_without_envflag", credentialProbe.terminationStatus != 0 && text.contains("unavailable in the sandbox preview"))
} catch { record("immutable_policy_blocks_credential_mode_without_envflag", false) }

manager.start { result in
    guard case .success(let base) = result else { finish("Sandbox collector failed to start: " + String(describing: result)); return }
    let first = manager.process!
    let token = manager.accessToken
    record("inherited_packaged_collector_started", first.isRunning)
    record("owned_process_group", getpgid(first.processIdentifier) == first.processIdentifier)
    record("loopback_requires_authentication", request(base.appendingPathComponent("api/state"), token: nil).0 == 403)
    let snapshot = request(base.appendingPathComponent("api/state"), token: token)
    record("authenticated_loopback_state", snapshot.0 == 200)
    record("unselected_sources_read_no_sessions", (snapshot.1["sessions"] as? [[String: Any]])?.isEmpty == true)
    let sourceHealth = snapshot.1["sources"] as? [[String: Any]] ?? []
    record("unselected_sources_report_disconnected", sourceHealth.count == 2 && sourceHealth.allSatisfy { $0["available"] as? Bool == false })
    let usage = snapshot.1["usage"] as? [[String: Any]] ?? []
    record("account_usage_unavailable", !usage.isEmpty && usage.allSatisfy { $0["available"] as? Bool == false })
    report["collector_pid"] = first.processIdentifier
    report["parent_pid"] = getpid()
    report["port"] = base.port!
    report["launch_artifact_names"] = Array(launchArtifacts().subtracting(initialLaunchArtifacts)).sorted()
    if mode == "sample-mode-isolation" {
        manager.stopAndWait {
            record("live_child_reaped_before_sample", !first.isRunning && kill(first.processIdentifier, 0) != 0)
            let liveDatabase = support.appendingPathComponent("data/board.sqlite3")
            guard let originalDatabase = try? Data(contentsOf: liveDatabase) else { finish("Live fixture database is missing."); return }
            do { try manager.setSampleMode(true) }
            catch { finish("Sample storage mode could not be selected."); return }
            let sampleSupport = manager.support
            record("sample_support_is_separate", sampleSupport == support.appendingPathComponent("Samples", isDirectory: true))
            record("sample_mode_clears_source_grants", manager.grantedSourceURLs.isEmpty)
            manager.start { sampled in
                guard case .success(let sampleBase) = sampled else { finish("Packaged sample collector failed to start."); return }
                let sampledPID = manager.process!.processIdentifier
                let sampleToken = manager.accessToken
                let state = request(sampleBase.appendingPathComponent("api/state"), token: sampleToken)
                record("packaged_sample_mode_authenticated", state.0 == 200 && state.1["sample_mode"] as? Bool == true)
                record("sample_rejects_live_access_token", request(sampleBase.appendingPathComponent("api/state"), token: token).0 == 403)
                let sources = state.1["sources"] as? [[String: Any]] ?? []
                record("sample_health_claims_only_sample_data", sources.count == 1 && sources.first?["id"] as? String == "samples")
                let sampleUsage = state.1["usage"] as? [[String: Any]] ?? []
                record("packaged_usage_explicitly_sample", sampleUsage.count == 2 && sampleUsage.allSatisfy { $0["source"] as? String == "sample" })
                let csrf = state.1["csrf_token"] as? String
                let route = sampleBase.appendingPathComponent("api/samples/advance")
                record("packaged_sample_question_action", request(route, token: sampleToken, body: ["action": "needs_you"], csrf: csrf).0 == 200)
                let question = request(sampleBase.appendingPathComponent("api/state"), token: sampleToken)
                let questionRows = question.1["sessions"] as? [[String: Any]] ?? []
                record("packaged_sample_question_rows", questionRows.count == 2 && questionRows.allSatisfy { $0["status"] as? String == "needs_you" })
                record("packaged_sample_ready_action", request(route, token: sampleToken, body: ["action": "ready"], csrf: csrf).0 == 200)
                let ready = request(sampleBase.appendingPathComponent("api/state"), token: sampleToken)
                let rows = ready.1["sessions"] as? [[String: Any]] ?? []
                record("packaged_sample_ready_rows", rows.count == 2 && rows.allSatisfy { $0["status"] as? String == "ready" })
                record("packaged_sample_rows_disable_provider_open", rows.count == 2 && rows.allSatisfy { $0["can_open"] as? Bool == false })
                record("packaged_sample_rows_disable_notifications", rows.count == 2 && rows.allSatisfy { $0["notification_eligible"] as? Bool == false })
                let openURL = sampleBase.appendingPathComponent("api/sessions/codex:sample-codex/open")
                record("packaged_sample_open_route_rejected", request(openURL, token: sampleToken, body: [:], csrf: csrf).0 == 409)
                record("sample_database_separate_and_present", FileManager.default.fileExists(atPath: sampleSupport.appendingPathComponent("data/sample-board/board.sqlite3").path))
                record("live_database_unchanged_during_sample", (try? Data(contentsOf: liveDatabase)) == originalDatabase)
                manager.stopAndWait {
                    record("sample_child_reaped_before_return", kill(sampledPID, 0) != 0)
                    do { try manager.setSampleMode(false) }
                    catch { finish("Live mode could not be restored."); return }
                    record("live_support_restored", manager.support == support)
                    manager.start { restored in
                        guard case .success(let liveBase) = restored else { finish("Live fixture collector did not restart."); return }
                        let restoredState = request(liveBase.appendingPathComponent("api/state"), token: manager.accessToken)
                        record("return_live_mode_not_sample", restoredState.0 == 200 && restoredState.1["sample_mode"] as? Bool == false)
                        record("sample_chats_never_enter_live_board", (restoredState.1["sessions"] as? [[String: Any]])?.isEmpty == true)
                        manager.stopAndWait {
                            record("live_return_private_files_removed", launchArtifacts().subtracting(initialLaunchArtifacts).isEmpty)
                            let remaining = (try? FileManager.default.contentsOfDirectory(atPath: sampleSupport.appendingPathComponent("data").path)) ?? []
                            record("sample_private_launch_files_removed", remaining.allSatisfy { !$0.hasPrefix(".collector-access-") && !$0.hasPrefix(".source-bookmarks-") })
                            finish()
                        }
                    }
                }
            }
        }
        return
    }
    if mode == "forced-owner-death" {
        save(runningURL, report)
        return
    }
    if mode == "collector-crash-retry" {
        var unexpectedExits = 0
        let marker = support.appendingPathComponent("data/recovery-fixture-" + runID + ".txt")
        let markerBytes = Data("Disposable recovery marker. No account or chat data.".utf8)
        do { try markerBytes.write(to: marker) }
        catch { finish("The recovery marker could not be saved."); return }
        manager.unexpectedExit = {
            unexpectedExits += 1
            record("ready_collector_crash_notified_once", unexpectedExits == 1)
            record("crash_cleared_owned_process", manager.process == nil)
            record("crashed_collector_reaped", kill(first.processIdentifier, 0) != 0)
            record("crash_removed_private_launch_files", launchArtifacts().subtracting(initialLaunchArtifacts).isEmpty)
            record("crash_closed_loopback_server", request(base.appendingPathComponent("api/health"), token: token).0 == 0)
            manager.start { retried in
                guard case .success(let recovered) = retried else { finish("Retry after collector crash failed."); return }
                record("retry_after_crash_started_collector", manager.process?.isRunning == true)
                record("retry_after_crash_kept_origin", recovered == base)
                record("retry_after_crash_rotated_access", manager.accessToken != token)
                record("retry_after_crash_rejected_old_access", request(recovered.appendingPathComponent("api/state"), token: token).0 == 403)
                let state = request(recovered.appendingPathComponent("api/state"), token: manager.accessToken)
                record("retry_after_crash_authenticated", state.0 == 200)
                let sources = state.1["sources"] as? [[String: Any]] ?? []
                record("retry_after_crash_did_not_grant_sources", sources.count == 2 && sources.allSatisfy { $0["available"] as? Bool == false })
                record("retry_after_crash_preserved_local_file", (try? Data(contentsOf: marker)) == markerBytes)
                let recoveredPID = manager.process!.processIdentifier
                manager.stop()
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                    record("retry_stop_reaped_collector", kill(recoveredPID, 0) != 0)
                    record("retry_stop_removed_private_launch_files", launchArtifacts().subtracting(initialLaunchArtifacts).isEmpty)
                    record("intentional_retry_stop_did_not_notify_crash", unexpectedExits == 1)
                    try? FileManager.default.removeItem(at: marker)
                    finish()
                }
            }
        }
        if kill(first.processIdentifier, SIGKILL) != 0 { finish("The disposable collector crash could not be triggered.") }
        return
    }
    manager.start { second in
        guard case .success(let next) = second else { finish("Sandbox collector restart failed."); return }
        record("restart_reaped_previous_collector", !first.isRunning)
        record("restart_rotated_access_token", token != manager.accessToken)
        record("previous_token_rejected", request(next.appendingPathComponent("api/state"), token: token).0 == 403)
        let pid = manager.process!.processIdentifier
        manager.stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
            record("normal_stop_reaped_collector", kill(pid, 0) != 0)
            record("normal_stop_closed_loopback_server", request(next.appendingPathComponent("api/health"), token: nil).0 == 0)
            record("private_launch_files_removed", launchArtifacts().subtracting(initialLaunchArtifacts).isEmpty)
            finish()
        }
    }
}
DispatchQueue.main.asyncAfter(deadline: .now() + 75) { finish("Sandbox proof timed out.") }
RunLoop.main.run()
