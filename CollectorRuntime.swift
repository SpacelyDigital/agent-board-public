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

/// A launch marker is locked by both owner and collector. Only an orphaned
/// marker grants startup cleanup rights; older unmarked launches are preserved.
final class NativeLaunchLease {
    private let path: URL
    private var directory: Int32
    private var descriptor: Int32
    private let identity: (dev_t, ino_t)
    private let name: String
    private let instance: String

    private static func privateFile(_ value: stat, limit: Int64) -> Bool {
        value.st_mode & S_IFMT == S_IFREG && value.st_mode & 0o7777 == 0o600
            && value.st_uid == getuid() && value.st_nlink == 1
            && value.st_size >= 0 && value.st_size <= limit
    }

    private static func sameFile(_ left: stat, _ right: stat) -> Bool {
        left.st_dev == right.st_dev && left.st_ino == right.st_ino
            && left.st_size == right.st_size && left.st_mode == right.st_mode
            && left.st_uid == right.st_uid && left.st_nlink == right.st_nlink
            && left.st_mtimespec.tv_sec == right.st_mtimespec.tv_sec
            && left.st_mtimespec.tv_nsec == right.st_mtimespec.tv_nsec
            && left.st_ctimespec.tv_sec == right.st_ctimespec.tv_sec
            && left.st_ctimespec.tv_nsec == right.st_ctimespec.tv_nsec
    }

    private static func sameDirectory(_ descriptor: Int32, path: URL) -> Bool {
        var opened = stat(), current = stat()
        return fstat(descriptor, &opened) == 0 && lstat(path.path, &current) == 0
            && current.st_mode & S_IFMT == S_IFDIR && current.st_mode & 0o7777 == 0o700
            && current.st_uid == getuid() && current.st_dev == opened.st_dev && current.st_ino == opened.st_ino
    }

    private static func removePrivate(_ name: String, directory: Int32, path: URL, limit: Int64) -> Bool {
        guard sameDirectory(directory, path: path) else { return false }
        let file = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        if file < 0 { return errno == ENOENT }
        defer { Darwin.close(file) }
        var opened = stat(), current = stat()
        guard fstat(file, &opened) == 0, privateFile(opened, limit: limit),
              fstatat(directory, name, &current, AT_SYMLINK_NOFOLLOW) == 0,
              sameFile(opened, current), sameDirectory(directory, path: path) else { return false }
        return unlinkat(directory, name, 0) == 0
    }

    private static func instance(from name: String) -> String? {
        let prefix = ".collector-launch-", suffix = ".lock"
        guard name.hasPrefix(prefix), name.hasSuffix(suffix) else { return nil }
        let value = String(name.dropFirst(prefix.count).dropLast(suffix.count))
        guard let uuid = UUID(uuidString: value), uuid.uuidString.lowercased() == value.lowercased() else { return nil }
        return value
    }

    static func markerMatchesDescriptor(_ descriptor: Int32, directory: Int32, name: String, path: URL) -> Bool {
        var opened = stat(), current = stat()
        return instance(from: name) != nil && sameDirectory(directory, path: path)
            && fstat(descriptor, &opened) == 0 && privateFile(opened, limit: 0)
            && fstatat(directory, name, &current, AT_SYMLINK_NOFOLLOW) == 0 && sameFile(opened, current)
    }

    @discardableResult
    static func sweep(in path: URL) throws -> [String] {
        let directory = open(path.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directory >= 0 else { throw CollectorRuntime.RuntimeError.unavailable }
        defer { Darwin.close(directory) }
        guard sameDirectory(directory, path: path) else { throw CollectorRuntime.RuntimeError.unavailable }
        var removed: [String] = []
        for name in try FileManager.default.contentsOfDirectory(atPath: path.path) {
            guard let instance = instance(from: name), sameDirectory(directory, path: path) else { continue }
            let lock = openat(directory, name, O_RDWR | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
            guard lock >= 0 else { continue }
            defer { Darwin.close(lock) }
            var opened = stat(), current = stat()
            guard fstat(lock, &opened) == 0, privateFile(opened, limit: 0),
                  flock(lock, LOCK_EX | LOCK_NB) == 0,
                  fstatat(directory, name, &current, AT_SYMLINK_NOFOLLOW) == 0,
                  sameFile(opened, current), sameDirectory(directory, path: path) else { continue }
            let token = removePrivate(".collector-access-" + instance, directory: directory, path: path, limit: 128)
            let manifest = removePrivate(".source-bookmarks-" + instance + ".json", directory: directory, path: path, limit: 262_144)
            // Preserve the marker when any associated entry was substituted or
            // shared. Do not turn an uncertain leftover into an unmarked file.
            if token && manifest && removePrivate(name, directory: directory, path: path, limit: 0) {
                removed.append(instance)
            }
        }
        return removed
    }

    init(in path: URL, instance: String) throws {
        guard UUID(uuidString: instance)?.uuidString.lowercased() == instance.lowercased() else {
            throw CollectorRuntime.RuntimeError.unavailable
        }
        self.path = path
        self.instance = instance
        name = ".collector-launch-" + instance + ".lock"
        directory = open(path.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        descriptor = -1
        var info = stat()
        guard directory >= 0, Self.sameDirectory(directory, path: path), fstat(directory, &info) == 0 else {
            if directory >= 0 { Darwin.close(directory) }
            directory = -1
            throw CollectorRuntime.RuntimeError.unavailable
        }
        identity = (info.st_dev, info.st_ino)
        descriptor = openat(directory, name, O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0o600)
        guard descriptor >= 0, fchmod(descriptor, 0o600) == 0,
              flock(descriptor, LOCK_SH | LOCK_NB) == 0,
              // A simultaneous startup sweep may have removed this just-created
              // marker before the shared lock was acquired. Never launch using
              // a lock on an unlinked or substituted inode.
              Self.markerMatchesDescriptor(descriptor, directory: directory, name: name, path: path) else {
            if descriptor >= 0 { Darwin.close(descriptor) }
            Darwin.close(directory)
            descriptor = -1; directory = -1
            throw CollectorRuntime.RuntimeError.unavailable
        }
    }

    func finish() {
        guard descriptor >= 0 else { return }
        defer { Darwin.close(descriptor); Darwin.close(directory); descriptor = -1; directory = -1 }
        guard Self.sameDirectory(directory, path: path), flock(descriptor, LOCK_EX | LOCK_NB) == 0 else { return }
        var current = stat(), opened = stat(), parent = stat()
        guard fstat(directory, &parent) == 0, (parent.st_dev, parent.st_ino) == identity,
              fstat(descriptor, &opened) == 0,
              fstatat(directory, name, &current, AT_SYMLINK_NOFOLLOW) == 0,
              Self.privateFile(opened, limit: 0), Self.sameFile(opened, current) else { return }
        let token = Self.removePrivate(".collector-access-" + instance, directory: directory, path: path, limit: 128)
        let manifest = Self.removePrivate(".source-bookmarks-" + instance + ".json", directory: directory, path: path, limit: 262_144)
        if token && manifest { _ = Self.removePrivate(name, directory: directory, path: path, limit: 0) }
    }

    deinit { finish() }
}

/// Owns the bundled collector. Closing the window keeps the app and collector
/// alive; quitting the app stops only the child started by this instance.
final class CollectorRuntime {
    enum RuntimeError: LocalizedError {
        case missing, unavailable, startupTimedOut, exitedDuringStartup, launchFailed, legacy(String)
        var errorDescription: String? {
            switch self {
            case .missing: return "The collector is missing from this copy of Agent Board. Download a complete copy of the app."
            case .unavailable: return "The local collector could not start. You can retry or open its local log. Your saved board has not been removed."
            case .startupTimedOut: return "The local collector did not become ready within five minutes. Complete any macOS access request, then retry monitoring. Your saved board has not been removed."
            case .exitedDuringStartup: return "The local collector stopped before it was ready. You can retry or open its local log. Your saved board has not been removed."
            case .launchFailed: return "The local collector could not be prepared or launched. You can retry or open its local log. Your saved board has not been removed."
            case .legacy(let detail): return detail
            }
        }
    }

    struct StartupTiming {
        let softAttempts: Int
        let hardSeconds: TimeInterval
        let retrySeconds: TimeInterval
        let requestSeconds: TimeInterval
        static let production = StartupTiming(softAttempts: 100, hardSeconds: 300,
                                              retrySeconds: 0.25, requestSeconds: 1)
    }

    enum StartupPhase: String {
        case restoringSources = "restore_sources"
        case sourcesRestored = "sources_restored"
        case preparing = "prepare"
        case transferringSources = "transfer_sources"
        case launching = "launch"
        case checkingHealth = "health"
        case waiting = "still_starting"
        case ready = "ready"
        case exited = "child_exited"
        case timedOut = "hard_timeout"
        case cancelled = "cancelled"
        case failed = "failed"
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
    private let startupTiming: StartupTiming
    private let healthSession: URLSession?
    private let portProvider: (() -> Int)?
    private var startupBegan: UInt64?
    private var startupDeadline: DispatchWorkItem?
    private var startupRetry: DispatchWorkItem?
    private var healthRequestID: UUID?
    private var reportedWaiting = false
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
    private var launchLease: NativeLaunchLease?
    var usageEnabled = false
    var projectsEnabled = false
    var claudeChatsEnabled = false
    var chatGPTChatsEnabled = false
    private var attempts = 0
    private var ready = false
    private var ownedGroup: pid_t?
    var unexpectedExit: (() -> Void)?
    var startupWaiting: (() -> Void)?
    var hasBundle: Bool { FileManager.default.isExecutableFile(atPath: executable.path) }
    var logURL: URL { support.appendingPathComponent("data/collector.log") }
    var legacyPlist: URL {
        legacyOverride ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents/local.agent-board.plist")
    }
    var needsLegacyMigration: Bool { !sandboxed && FileManager.default.fileExists(atPath: legacyPlist.path) }

    init(bundle: Bundle = .main, support: URL? = nil,
         sourceRoots: (codex: URL, claude: URL)? = nil, legacyPlist: URL? = nil, preferredPort: Int = 8769,
         sandboxed: Bool? = nil, startupTiming: StartupTiming = .production,
         healthSession: URLSession? = nil, portProvider: (() -> Int)? = nil) {
        self.sandboxed = sandboxed ?? (bundle.object(forInfoDictionaryKey: "AgentBoardSandboxed") as? Bool ?? false)
        self.storeDistribution = bundle.object(forInfoDictionaryKey: "AgentBoardStoreDistribution") as? Bool ?? false
        self.liveSupport = support ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Agent Board")
        executable = bundle.bundleURL.appendingPathComponent("Contents/Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector")
        self.sourceRoots = sourceRoots
        legacyOverride = legacyPlist
        self.preferredPort = preferredPort
        self.startupTiming = startupTiming
        self.healthSession = healthSession
        self.portProvider = portProvider
        precondition(startupTiming.softAttempts > 0 && startupTiming.hardSeconds.isFinite
                     && startupTiming.hardSeconds > 0 && startupTiming.retrySeconds.isFinite
                     && startupTiming.retrySeconds > 0 && startupTiming.requestSeconds.isFinite
                     && startupTiming.requestSeconds > 0)
    }

    // These fixed stages and bounded error codes are local diagnostics only.
    // Never include source URLs, bookmark bytes, tokens, chat text or error text.
    func recordStartupPhase(_ phase: StartupPhase, error: Error? = nil) {
        let failure = error as NSError?
        let knownDomains = [NSCocoaErrorDomain, NSPOSIXErrorDomain, NSOSStatusErrorDomain]
        let domain = failure.map { knownDomains.contains($0.domain) ? $0.domain : "other" } ?? "none"
        let line = "Agent Board startup phase=\(phase.rawValue) error_domain=\(domain) error_code=\(failure?.code ?? 0)\n"
        if let logHandle {
            if (try? logHandle.write(contentsOf: Data(line.utf8))) != nil { return }
        } else {
            let data = support.appendingPathComponent("data", isDirectory: true)
            do {
                if sampleMode { try validateSampleStorage() }
                try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true,
                                                        attributes: [.posixPermissions: 0o700])
                let handle = try Self.openCollectorLog(in: data)
                defer { try? handle.close() }
                try handle.write(contentsOf: Data(line.utf8))
                return
            } catch { /* A failed diagnostic must not change source access. */ }
        }
        NSLog("%@", line.trimmingCharacters(in: .newlines))
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

    // Open only the app's own single-link regular log. Keep operations anchored
    // to its directory descriptor, and never recursively remove rotation paths.
    static func openCollectorLog(in directory: URL) throws -> FileHandle {
        let flags = O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC
        let parent = Darwin.open(directory.path, O_RDONLY | O_DIRECTORY | flags)
        guard parent >= 0 else { throw RuntimeError.unavailable }
        defer { Darwin.close(parent) }
        var parentInfo = stat()
        guard fstat(parent, &parentInfo) == 0,
              (parentInfo.st_mode & S_IFMT) == S_IFDIR, parentInfo.st_uid == getuid() else {
            throw RuntimeError.unavailable
        }
        func checked(_ descriptor: Int32, name: String) throws -> stat {
            var info = stat()
            var named = stat()
            guard fstat(descriptor, &info) == 0,
                  (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid(), info.st_nlink == 1,
                  fstatat(parent, name, &named, AT_SYMLINK_NOFOLLOW) == 0,
                  named.st_dev == info.st_dev, named.st_ino == info.st_ino,
                  (named.st_mode & S_IFMT) == S_IFREG, named.st_uid == getuid(), named.st_nlink == 1 else {
                throw RuntimeError.unavailable
            }
            return info
        }
        var descriptor = openat(parent, "collector.log", O_WRONLY | O_APPEND | O_CREAT | flags, 0o600)
        guard descriptor >= 0 else { throw RuntimeError.unavailable }
        defer { if descriptor >= 0 { Darwin.close(descriptor) } }
        let info = try checked(descriptor, name: "collector.log")
        guard fchmod(descriptor, 0o600) == 0 else { throw RuntimeError.unavailable }
        if info.st_size > 2_000_000 {
            var previousInfo = stat()
            let existing = fstatat(parent, "collector.previous.log", &previousInfo, AT_SYMLINK_NOFOLLOW)
            var previous: Int32 = -1
            defer { if previous >= 0 { Darwin.close(previous) } }
            if existing == 0 {
                previous = openat(parent, "collector.previous.log", O_RDONLY | flags)
                guard previous >= 0 else { throw RuntimeError.unavailable }
                _ = try checked(previous, name: "collector.previous.log")
            } else if errno != ENOENT {
                throw RuntimeError.unavailable
            }
            // Recheck both names immediately before the single nonrecursive
            // rename. Directories, links and special files are never removed.
            _ = try checked(descriptor, name: "collector.log")
            if previous >= 0 { _ = try checked(previous, name: "collector.previous.log") }
            let renamed = existing == 0
                ? renameat(parent, "collector.log", parent, "collector.previous.log")
                : renameatx_np(parent, "collector.log", parent, "collector.previous.log", UInt32(RENAME_EXCL))
            guard renamed == 0 else { throw RuntimeError.unavailable }
            _ = try checked(descriptor, name: "collector.previous.log")
            Darwin.close(descriptor)
            descriptor = openat(parent, "collector.log", O_WRONLY | O_APPEND | O_CREAT | O_EXCL | flags, 0o600)
            guard descriptor >= 0 else { throw RuntimeError.unavailable }
            _ = try checked(descriptor, name: "collector.log")
            guard fchmod(descriptor, 0o600) == 0 else { throw RuntimeError.unavailable }
        }
        _ = try checked(descriptor, name: "collector.log")
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        descriptor = -1
        return handle
    }

    private func sandboxSources(data: URL) throws -> [String: URL] {
        var sources: [String: [String: String]] = [:]
        var roots: [String: URL] = [:]
        for provider in ["codex", "claude", "claude_desktop"] {
            if !sampleMode, let granted = grantedSourceURLs[provider] {
                let url = granted.resolvingSymlinksInPath().standardizedFileURL
                guard provider != "claude_desktop" || url.lastPathComponent == "claude-code-sessions" else {
                    throw RuntimeError.legacy("Choose the Claude Code sessions folder in Setup and Permissions. The previous Claude app folder permission is no longer used.")
                }
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
        let current = generation
        startupBegan = DispatchTime.now().uptimeNanoseconds
        scheduleStartupDeadline(generation: current, completion: completion)
        recordStartupPhase(.preparing)
        do {
            if sampleMode { try validateSampleStorage() }
            let data = support.appendingPathComponent("data")
            try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: support.path)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: data.path)
            logHandle = try Self.openCollectorLog(in: data)
            let child = Process()
            let lifetime = Pipe()
            _ = fcntl(lifetime.fileHandleForWriting.fileDescriptor, F_SETFD, FD_CLOEXEC)
            lifetimeWriter = lifetime.fileHandleForWriting
            child.standardInput = lifetime.fileHandleForReading
            port = portProvider?() ?? availablePort()
            instanceID = UUID().uuidString
            try NativeLaunchLease.sweep(in: data)
            launchLease = try NativeLaunchLease(in: data, instance: instanceID)
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
            if !sandboxed && !storeDistribution && !sampleMode && sourceRoots == nil {
                if claudeChatsEnabled {
                    child.arguments?.append("--claude-chats")
                }
                if chatGPTChatsEnabled {
                    child.arguments?.append("--chatgpt-chats")
                }
            }
            if sandboxed {
                recordStartupPhase(.transferringSources)
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
            child.terminationHandler = { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self, current == self.generation else { return }
                    if self.ready {
                        self.stop()
                        self.unexpectedExit?()
                    } else {
                        self.failStartup(.exitedDuringStartup, phase: .exited,
                                         generation: current, completion: completion)
                    }
                }
            }
            recordStartupPhase(.launching)
            try child.run()
            try? lifetime.fileHandleForReading.close()
            process = child
            attempts = 0
            recordStartupPhase(.checkingHealth)
            checkHealth(generation: current, completion: completion)
        } catch {
            recordStartupPhase(.failed, error: error)
            startupBegan = nil
            stop()
            completion(.failure(RuntimeError.launchFailed))
        }
    }

    private var startupSecondsRemaining: TimeInterval {
        guard let began = startupBegan else { return 0 }
        let now = DispatchTime.now().uptimeNanoseconds
        return startupTiming.hardSeconds - Double(now >= began ? now - began : 0) / 1_000_000_000
    }

    private func failStartup(_ error: RuntimeError, phase: StartupPhase, generation expected: UUID,
                             completion: @escaping (Result<URL, Error>) -> Void) {
        guard expected == generation, !ready else { return }
        recordStartupPhase(phase)
        startupBegan = nil
        stop()
        completion(.failure(error))
    }

    private func scheduleStartupDeadline(generation expected: UUID,
                                         completion: @escaping (Result<URL, Error>) -> Void) {
        startupDeadline?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, expected == self.generation, !self.ready else { return }
            if self.startupSecondsRemaining > 0 {
                self.scheduleStartupDeadline(generation: expected, completion: completion)
                return
            }
            self.failStartup(.startupTimedOut, phase: .timedOut,
                             generation: expected, completion: completion)
        }
        startupDeadline = work
        DispatchQueue.main.asyncAfter(deadline: .now() + max(0, startupSecondsRemaining), execute: work)
    }

    private func checkHealth(generation expected: UUID, completion: @escaping (Result<URL, Error>) -> Void) {
        guard expected == generation, !ready, startupTask == nil else { return }
        guard startupSecondsRemaining > 0 else {
            failStartup(.startupTimedOut, phase: .timedOut, generation: expected, completion: completion)
            return
        }
        guard process?.isRunning == true else {
            failStartup(.exitedDuringStartup, phase: .exited, generation: expected, completion: completion)
            return
        }
        if attempts >= startupTiming.softAttempts, !reportedWaiting {
            reportedWaiting = true
            recordStartupPhase(.waiting)
            startupWaiting?()
            // The callback may have cancelled or replaced this launch.
            guard expected == generation else { return }
        }
        attempts += 1
        let base = URL(string: "http://127.0.0.1:\(port)/")!
        var request = URLRequest(url: base.appendingPathComponent("api/health"))
        request.timeoutInterval = min(startupTiming.requestSeconds, startupSecondsRemaining)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue(accessToken, forHTTPHeaderField: "X-Agent-Board-Access")
        let requestID = UUID()
        healthRequestID = requestID
        startupTask = (healthSession ?? session).dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self, expected == self.generation, requestID == self.healthRequestID else { return }
                self.startupTask = nil
                self.healthRequestID = nil
                guard self.startupSecondsRemaining > 0 else {
                    self.failStartup(.startupTimedOut, phase: .timedOut,
                                     generation: expected, completion: completion)
                    return
                }
                guard self.process?.isRunning == true else {
                    self.failStartup(.exitedDuringStartup, phase: .exited,
                                     generation: expected, completion: completion)
                    return
                }
                let value = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                if error == nil, (response as? HTTPURLResponse)?.statusCode == 200,
                   value?["app"] as? String == "agent-board", value?["instance_id"] as? String == self.instanceID {
                    self.ready = true
                    self.startupBegan = nil
                    self.startupDeadline?.cancel()
                    self.startupDeadline = nil
                    self.recordStartupPhase(.ready)
                    self.preferredPort = self.port
                    if let pid = self.process?.processIdentifier, getpgid(pid) == pid, pid != getpgrp() {
                        self.ownedGroup = pid
                    }
                    completion(.success(base))
                } else {
                    let retry = DispatchWorkItem { [weak self] in
                        guard let self, expected == self.generation else { return }
                        self.startupRetry = nil
                        self.checkHealth(generation: expected, completion: completion)
                    }
                    self.startupRetry = retry
                    DispatchQueue.main.asyncAfter(deadline: .now() + min(self.startupTiming.retrySeconds,
                                                                        self.startupSecondsRemaining), execute: retry)
                }
            }
        }
        startupTask?.resume()
    }

    func stop() {
        if startupBegan != nil { recordStartupPhase(.cancelled) }
        generation = UUID()
        ready = false
        startupBegan = nil
        reportedWaiting = false
        startupDeadline?.cancel()
        startupDeadline = nil
        startupRetry?.cancel()
        startupRetry = nil
        healthRequestID = nil
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
        accessTokenFile = nil
        sourceManifestFile = nil
        launchLease?.finish()
        launchLease = nil
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
