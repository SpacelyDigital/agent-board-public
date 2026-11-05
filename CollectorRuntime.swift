import Foundation
import Darwin

final class LocalSessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        // Local bearer headers must never follow a redirect to another origin.
        completionHandler(nil)
    }
}

/// Owns the bundled collector. Closing the window keeps the app and collector
/// alive; quitting the app stops only the child started by this instance.
final class CollectorRuntime {
    enum RuntimeError: LocalizedError {
        case missing, unavailable, legacy(String)
        var errorDescription: String? {
            switch self {
            case .missing: return "The collector is missing from this copy of Agent Board. Download a complete copy of the app."
            case .unavailable: return "The local collector could not start. You can retry or open its local log. Your saved board has not been removed."
            case .legacy(let detail): return detail
            }
        }
    }

    private let liveSupport: URL
    var support: URL { sampleMode ? liveSupport.appendingPathComponent("Samples", isDirectory: true) : liveSupport }
    let executable: URL
    var process: Process?
    var startupTask: URLSessionDataTask?
    private var generation = UUID()
    private var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        return URLSession(configuration: configuration, delegate: LocalSessionDelegate(), delegateQueue: nil)
    }()
    private let sourceRoots: (codex: URL, claude: URL)?
    let sandboxed: Bool
    let storeDistribution: Bool
    private(set) var sampleMode = false
    var grantedSourceURLs: [String: URL] = [:]
    private let legacyOverride: URL?
    var preferredPort: Int
    private var logHandle: FileHandle?
    private var lifetimeWriter: FileHandle?
    private var port = 8769
    private var instanceID = UUID().uuidString
    private(set) var accessToken = ""
    private var accessTokenFile: URL?
    private var sourceManifestFile: URL?
    var usageEnabled = false
    var projectsEnabled = false
    private var attempts = 0
    private var ready = false
    private var ownedGroup: pid_t?
    var unexpectedExit: (() -> Void)?
    var hasBundle: Bool { FileManager.default.isExecutableFile(atPath: executable.path) }
    var logURL: URL { support.appendingPathComponent("data/collector.log") }
    var legacyPlist: URL {
        legacyOverride ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents/local.agent-board.plist")
    }
    var needsLegacyMigration: Bool { !sandboxed && FileManager.default.fileExists(atPath: legacyPlist.path) }

    init(bundle: Bundle = .main, support: URL? = nil,
         sourceRoots: (codex: URL, claude: URL)? = nil, legacyPlist: URL? = nil, preferredPort: Int = 8769,
         sandboxed: Bool? = nil) {
        self.sandboxed = sandboxed ?? (bundle.object(forInfoDictionaryKey: "AgentBoardSandboxed") as? Bool ?? false)
        self.storeDistribution = bundle.object(forInfoDictionaryKey: "AgentBoardStoreDistribution") as? Bool ?? false
        self.liveSupport = support ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Agent Board")
        executable = bundle.bundleURL.appendingPathComponent("Contents/Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector")
        self.sourceRoots = sourceRoots
        legacyOverride = legacyPlist
        self.preferredPort = preferredPort
    }

    func setSampleMode(_ enabled: Bool) throws {
        guard !enabled || (sandboxed && storeDistribution), process == nil else { throw RuntimeError.unavailable }
        if enabled { try validateSampleStorage() }
        sampleMode = enabled
        grantedSourceURLs.removeAll()
    }

    private func validateSampleStorage() throws {
        let data = liveSupport.appendingPathComponent("Samples/data", isDirectory: true)
        guard data.isFileURL, data.path.hasPrefix("/"), !data.pathComponents.contains("..") else {
            throw RuntimeError.unavailable
        }
        var cursor = URL(fileURLWithPath: "/", isDirectory: true)
        for component in data.pathComponents.dropFirst() {
            cursor.appendPathComponent(component, isDirectory: true)
            guard let attributes = try? FileManager.default.attributesOfItem(atPath: cursor.path) else { continue }
            if attributes[.type] as? FileAttributeType == .typeSymbolicLink {
                let alias = ["/var": "/private/var", "/tmp": "/private/tmp"][cursor.path]
                guard let alias, cursor.resolvingSymlinksInPath().path == alias,
                      (attributes[.ownerAccountID] as? NSNumber)?.intValue == 0 else { throw RuntimeError.unavailable }
            }
        }
        let log = data.appendingPathComponent("collector.log")
        if let attributes = try? FileManager.default.attributesOfItem(atPath: log.path) {
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  (attributes[.referenceCount] as? NSNumber)?.intValue == 1,
                  (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid() else { throw RuntimeError.unavailable }
        }
    }

    private func sandboxSources(data: URL) throws -> [String: URL] {
        var sources: [String: [String: String]] = [:]
        var roots: [String: URL] = [:]
        for provider in ["codex", "claude", "claude_desktop"] {
            if !sampleMode, let granted = grantedSourceURLs[provider] {
                let url = granted.resolvingSymlinksInPath().standardizedFileURL
                // A transferable bookmark is distinct from the persistent
                // app-scoped bookmark. The child consumes this grant itself.
                let transfer = try granted.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
                roots[provider] = url
                sources[provider] = ["path": url.path, "bookmark": transfer.base64EncodedString()]
            } else {
                let empty = support.appendingPathComponent("sources-unavailable/" + provider)
                let parent = empty.deletingLastPathComponent()
                if (try? parent.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true
                    || (try? empty.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
                    throw RuntimeError.unavailable
                }
                try FileManager.default.createDirectory(at: empty, withIntermediateDirectories: true,
                                                        attributes: [.posixPermissions: 0o700])
                guard try FileManager.default.contentsOfDirectory(atPath: empty.path).isEmpty else {
                    throw RuntimeError.unavailable
                }
                roots[provider] = empty
                sources[provider] = ["path": empty.path]
            }
        }
        let manifest = data.appendingPathComponent(".source-bookmarks-" + instanceID + ".json")
        try JSONSerialization.data(withJSONObject: ["version": 1, "sources": sources]).write(to: manifest, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: manifest.path)
        sourceManifestFile = manifest
        return roots
    }

    func migrateLegacyService() throws {
        guard needsLegacyMigration else { return }
        let contents = try Data(contentsOf: legacyPlist)
        let value = try PropertyListSerialization.propertyList(from: contents, format: nil) as? [String: Any]
        guard value?["Label"] as? String == "local.agent-board" else {
            throw RuntimeError.legacy("The old login service could not be identified safely. Remove it with the previous installer before continuing.")
        }
        let command = Process()
        command.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        command.arguments = ["bootout", "gui/\(getuid())", legacyPlist.path]
        command.standardOutput = FileHandle.nullDevice
        command.standardError = FileHandle.nullDevice
        try command.run()
        command.waitUntilExit()
        let inspect = Process()
        inspect.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        inspect.arguments = ["print", "gui/\(getuid())/local.agent-board"]
        inspect.standardOutput = FileHandle.nullDevice
        inspect.standardError = FileHandle.nullDevice
        try inspect.run()
        inspect.waitUntilExit()
        guard inspect.terminationStatus != 0 else {
            throw RuntimeError.legacy("The previous collector is still running. It has not been replaced. Quit the old copy of Agent Board and retry Setup and Permissions.")
        }
        // A bootout can report not-found when the service was already stopped.
        let backups = support.appendingPathComponent("migration-backups")
        try FileManager.default.createDirectory(at: backups, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let destination = backups.appendingPathComponent("local.agent-board-\(Int(Date().timeIntervalSince1970))-\(UUID().uuidString).plist")
        try FileManager.default.moveItem(at: legacyPlist, to: destination)
    }

    private func availablePort() -> Int {
        let socketFD = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard socketFD >= 0 else { return Int.random(in: 20000...50000) }
        defer { Darwin.close(socketFD) }
        var reuse: Int32 = 1
        _ = setsockopt(socketFD, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(preferredPort).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(socketFD, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        if result == 0 { return preferredPort }
        address.sin_port = 0
        let fallback = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(socketFD, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        var size = socklen_t(MemoryLayout<sockaddr_in>.size)
        if fallback == 0 {
            _ = withUnsafeMutablePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(socketFD, $0, &size) }
            }
            return Int(UInt16(bigEndian: address.sin_port))
        }
        return Int.random(in: 20000...50000)
    }

    func start(completion: @escaping (Result<URL, Error>) -> Void) {
        let previous = process
        stop()
        if let previous, previous.isRunning {
            let current = generation
            DispatchQueue.global().async { [weak self] in
                previous.waitUntilExit()
                DispatchQueue.main.async { [weak self] in
                    guard let self, current == self.generation else { return }
                    self.start(completion: completion)
                }
            }
            return
        }
        guard hasBundle else { completion(.failure(RuntimeError.missing)); return }
        guard !needsLegacyMigration else {
            completion(.failure(RuntimeError.legacy("Complete Setup and Permissions to replace the previous login service before starting the bundled collector.")))
            return
        }
        do {
            if sampleMode { try validateSampleStorage() }
            let data = support.appendingPathComponent("data")
            try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: support.path)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: data.path)
            if let attributes = try? FileManager.default.attributesOfItem(atPath: logURL.path),
               (attributes[.size] as? NSNumber)?.intValue ?? 0 > 2_000_000 {
                let previous = data.appendingPathComponent("collector.previous.log")
                try? FileManager.default.removeItem(at: previous)
                try? FileManager.default.moveItem(at: logURL, to: previous)
            }
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            logHandle = try FileHandle(forWritingTo: logURL)
            try logHandle?.seekToEnd()
            let child = Process()
            let lifetime = Pipe()
            _ = fcntl(lifetime.fileHandleForWriting.fileDescriptor, F_SETFD, FD_CLOEXEC)
            lifetimeWriter = lifetime.fileHandleForWriting
            child.standardInput = lifetime.fileHandleForReading
            port = availablePort()
            instanceID = UUID().uuidString
            child.executableURL = executable
            accessToken = (UUID().uuidString + UUID().uuidString).replacingOccurrences(of: "-", with: "").lowercased()
            let tokenFile = data.appendingPathComponent(".collector-access-\(instanceID)")
            try Data(accessToken.utf8).write(to: tokenFile, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tokenFile.path)
            accessTokenFile = tokenFile
            child.arguments = ["--data-dir", data.path, "--port", String(port), "--instance-id", instanceID,
                               "--access-token-file", tokenFile.path, "--parent-lifetime-stdin"]
            if storeDistribution { child.arguments?.append("--store-profile") }
            if sampleMode { child.arguments?.append("--sample-mode") }
            if sandboxed || !usageEnabled { child.arguments?.append("--no-usage") }
            else { child.arguments?.append("--usage") }
            if sandboxed || !projectsEnabled { child.arguments?.append("--no-projects") }
            else { child.arguments?.append("--projects") }
            if sandboxed {
                let roots = try sandboxSources(data: data)
                child.arguments?.append(contentsOf: ["--sandbox", "--codex-root", roots["codex"]!.path,
                                                    "--claude-root", roots["claude"]!.path,
                                                    "--claude-desktop-root", roots["claude_desktop"]!.path])
            } else if let roots = sourceRoots {
                child.arguments?.append(contentsOf: ["--codex-root", roots.codex.path, "--claude-root", roots.claude.path])
            }
            child.currentDirectoryURL = support
            var environment = ProcessInfo.processInfo.environment
            for name in Array(environment.keys) where name.hasPrefix("PYTHON") || name.hasPrefix("DYLD_") {
                environment.removeValue(forKey: name)
            }
            environment["PYTHONNOUSERSITE"] = "1"
            // An inherited shell environment must not opt a direct build into
            // sandbox bookkeeping, or redirect a sandbox child's grant file.
            environment.removeValue(forKey: "AGENT_BOARD_SOURCE_BOOKMARKS")
            environment.removeValue(forKey: "AGENT_BOARD_SANDBOX")
            environment.removeValue(forKey: "AGENT_BOARD_STORE_PROFILE")
            if sandboxed {
                environment["AGENT_BOARD_SANDBOX"] = "1"
                environment["AGENT_BOARD_SOURCE_BOOKMARKS"] = sourceManifestFile!.path
            }
            child.environment = environment
            child.standardOutput = logHandle
            child.standardError = logHandle
            let current = generation
            child.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self, current == self.generation, self.ready else { return }
                    self.ready = false
                    self.stop()
                    self.unexpectedExit?()
                }
            }
            try child.run()
            try? lifetime.fileHandleForReading.close()
            process = child
            attempts = 0
            checkHealth(generation: current, completion: completion)
        } catch { stop(); completion(.failure(error)) }
    }

    private func checkHealth(generation expected: UUID, completion: @escaping (Result<URL, Error>) -> Void) {
        guard expected == generation else { return }
        guard process?.isRunning == true, attempts < 100 else {
            stop(); completion(.failure(RuntimeError.unavailable)); return
        }
        attempts += 1
        let base = URL(string: "http://127.0.0.1:\(port)/")!
        var request = URLRequest(url: base.appendingPathComponent("api/health"))
        request.timeoutInterval = 1
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue(accessToken, forHTTPHeaderField: "X-Agent-Board-Access")
        startupTask = session.dataTask(with: request) { [weak self] data, response, _ in
            DispatchQueue.main.async {
                guard let self, expected == self.generation else { return }
                let value = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                if (response as? HTTPURLResponse)?.statusCode == 200,
                   value?["app"] as? String == "agent-board", value?["instance_id"] as? String == self.instanceID {
                    self.startupTask = nil
                    self.ready = true
                    self.preferredPort = self.port
                    if let pid = self.process?.processIdentifier, getpgid(pid) == pid, pid != getpgrp() {
                        self.ownedGroup = pid
                    }
                    completion(.success(base))
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
                        self?.checkHealth(generation: expected, completion: completion)
                    }
                }
            }
        }
        startupTask?.resume()
    }

    func stop() {
        generation = UUID()
        ready = false
        startupTask?.cancel()
        startupTask = nil
        try? lifetimeWriter?.close()
        lifetimeWriter = nil
        let child = process
        let livePID = child?.isRunning == true ? child?.processIdentifier : nil
        let group = ownedGroup ?? livePID.flatMap { getpgid($0) == $0 && $0 != getpgrp() ? $0 : nil }
        if let pid = group ?? livePID {
            let ownsGroup = group != nil && pid > 1 && pid != getpgrp()
            if ownsGroup { kill(-pid, SIGTERM) }
            else if child?.isRunning == true { child?.terminate() }
            // The group includes only this collector and its helper reads.
            // Check continuously until empty so a later, unrelated PID reuse
            // cannot be mistaken for an old collector that needs escalation.
            DispatchQueue.global().async {
                let deadline = Date().addingTimeInterval(7)
                if ownsGroup {
                    while kill(-pid, 0) == 0 && Date() < deadline { usleep(100_000) }
                    if kill(-pid, 0) == 0 { kill(-pid, SIGKILL) }
                } else {
                    while child?.isRunning == true && Date() < deadline { usleep(100_000) }
                    if child?.isRunning == true { kill(pid, SIGKILL) }
                }
            }
        }
        process = nil
        ownedGroup = nil
        if let tokenFile = accessTokenFile { try? FileManager.default.removeItem(at: tokenFile) }
        accessTokenFile = nil
        if let manifest = sourceManifestFile { try? FileManager.default.removeItem(at: manifest) }
        sourceManifestFile = nil
        try? logHandle?.close()
        logHandle = nil
    }

    func stopAndWait(completion: @escaping () -> Void) {
        let child = process
        stop()
        guard let child else { completion(); return }
        DispatchQueue.global(qos: .userInitiated).async {
            child.waitUntilExit()
            DispatchQueue.main.async { completion() }
        }
    }
}
