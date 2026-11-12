import AppKit
import WebKit
import UserNotifications
import CryptoKit
import ServiceManagement
import StoreKit
import Network
import Darwin

func existingChatURL(_ value: String, provider: String) -> URL? {
    guard value.utf8.count <= 1000 else { return nil }
    let pattern: String
    if provider == "codex" {
        pattern = #"^codex://threads/(?!new$)[A-Za-z0-9_-]{1,200}$"#
    } else if provider == "claude" {
        pattern = #"^claude://code/(?:continue\?session=local_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|project/chan_[A-Za-z0-9_]{6,120}(?:\?thread=cmsg_[A-Za-z0-9_]{6,120})?)$"#
    } else { return nil }
    guard value.range(of: pattern, options: .regularExpression) == value.startIndex..<value.endIndex else { return nil }
    return URL(string: value)
}

protocol AgentBoardLoginService {
    var status: SMAppService.Status { get }
    func register() throws
    func unregister() throws
}
extension SMAppService: AgentBoardLoginService {}

enum LoginPreferenceError: LocalizedError {
    case notFound, notConfirmed
    var errorDescription: String? {
        switch self {
        case .notFound: return "macOS could not locate Agent Board’s login item. Reopen your installed copy and try again."
        case .notConfirmed: return "macOS has not confirmed the login preference. Review Login Items in System Settings or try again later."
        }
    }
}

func loginStatusName(_ status: SMAppService.Status) -> String {
    switch status {
    case .notRegistered: return "notRegistered"
    case .enabled: return "enabled"
    case .requiresApproval: return "requiresApproval"
    case .notFound: return "notFound"
    @unknown default: return "unknown"
    }
}

func updateLoginPreference(enabled: Bool, service: AgentBoardLoginService,
                           onFailure: ((String) -> Void)? = nil) throws -> SMAppService.Status {
    let previous = service.status
    var current = previous
    var phase = "verify"
    var registrationAttempted = false
    var unregistrationAttempted = false
    do {
        if enabled && [.notRegistered, .notFound].contains(previous) {
            phase = "register"
            registrationAttempted = true
            try service.register()
        } else if !enabled && [.enabled, .requiresApproval].contains(previous) {
            phase = "unregister"
            unregistrationAttempted = true
            try service.unregister()
        }
        phase = "verify"
        current = service.status
        if enabled {
            guard current != .notFound else { throw LoginPreferenceError.notFound }
            guard [.enabled, .requiresApproval].contains(current) else { throw LoginPreferenceError.notConfirmed }
        } else if [.enabled, .requiresApproval].contains(current) {
            throw LoginPreferenceError.notConfirmed
        }
        return current
    } catch {
        if phase != "verify" { current = service.status }
        let failure = error as NSError
        let allowedDomains = ["SMAppServiceErrorDomain", NSCocoaErrorDomain, NSPOSIXErrorDomain, NSOSStatusErrorDomain]
        let domain = error is LoginPreferenceError ? "AgentBoardLoginPreference"
            : allowedDomains.contains(failure.domain) ? failure.domain : "other"
        // Never include error descriptions, userInfo, paths or account details.
        onFailure?("Agent Board login preference failed: enabled=\(enabled) phase=\(phase) before=\(loginStatusName(previous)) after=\(loginStatusName(current)) register_attempted=\(registrationAttempted) unregister_attempted=\(unregistrationAttempted) error_domain=\(domain) error_code=\(failure.code)")
        throw error
    }
}

enum AgentBoardAlertSound {
    static let ids = ["soft-ding", "chime", "bell", "glass", "marimba", "arcade", "radar", "space-signal", "pop", "fart"]

    static func filename(preferences: [String: Any]) -> String? {
        guard preferences["agent-board:sound"] as? String == "on" else { return nil }
        let saved = preferences["agent-board:sound-id"] as? String ?? "soft-ding"
        let selected = ids.contains(saved) ? saved : "soft-ding"
        return "agent-board-" + selected + ".wav"
    }
}

struct AttentionChat {
    let key: String
    let title: String
    let provider: String
    let status: String
    let eventID: String
    let canOpen: Bool
    let eligible: Bool
    let reconfirmationSilent: Bool
    let snoozedUntil: Double

    init?(_ value: [String: Any], now: Double, sampleMode: Bool = false) {
        guard let key = value["key"] as? String,
              let status = value["status"] as? String,
              status == "ready" || status == "needs_you" else { return nil }
        self.key = key
        self.title = value["title"] as? String ?? "Conversation"
        let providerNames = ["codex": "Codex", "claude": "Claude", "chatgpt": "ChatGPT"]
        self.provider = providerNames[value["provider"] as? String ?? ""] ?? "The agent"
        self.status = status
        self.eventID = value["event_id"] as? String ?? ""
        let sample = sampleMode && value["sample"] as? Bool == true
        self.canOpen = !sampleMode && (value["can_open"] as? Bool ?? false)
        self.snoozedUntil = (value["snoozed_until"] as? NSNumber)?.doubleValue ?? 0
        let background = value["scope"] as? String == "background" || value["is_background"] as? Bool == true
        let sourceUnavailable = value["source_unavailable"] as? Bool == true
        self.eligible = !background && !sourceUnavailable && self.snoozedUntil <= now
            && (sample || (value["notification_eligible"] as? Bool ?? true))
        self.reconfirmationSilent = value["reconfirmation_silent"] as? Bool ?? false
    }

    var signature: String { key + "|" + eventID }
}

struct AttentionDelivery {
    let generation: UUID
    let intent: UUID
    let snoozedUntil: Double
    let expiresAt: TimeInterval
    let snapshotExpiresAt: Double
    var attempt = UUID()
    var attempts = 0
    var retryAt: TimeInterval
    var inFlight = false
}

func fixedKoFiURL(_ value: String) -> URL? {
    guard value == "https://ko-fi.com/spacelydigital" else { return nil }
    return URL(string: value)
}

func externalSupportAvailable(strategy: String, storeBuild: Bool, countryCode: String?, connected: Bool) -> Bool {
    storeBuild && strategy == "us-external" && connected && countryCode == "USA"
}

@MainActor final class ExternalSupportController {
    let strategy: String
    let storeBuild: Bool
    let country: () async -> String?
    let canPresent: () -> Bool
    let render: (Bool) -> Void
    let open: () -> Void
    let now: () -> TimeInterval
    var connected = false
    private var generation = UUID()
    private var lookup: Task<Void, Never>?
    private(set) var allowed = false
    private var lastOpenAttempt: TimeInterval?

    init(strategy: String, storeBuild: Bool, country: @escaping () async -> String?,
         canPresent: @escaping () -> Bool, render: @escaping (Bool) -> Void,
         open: @escaping () -> Void, now: @escaping () -> TimeInterval = { Date().timeIntervalSince1970 }) {
        self.strategy = strategy; self.storeBuild = storeBuild; self.country = country
        self.canPresent = canPresent; self.render = render; self.open = open; self.now = now
    }

    func invalidate() {
        generation = UUID()
        lookup?.cancel()
        lookup = nil
        allowed = false
        render(false)
    }

    func refresh(openAfterCheck: Bool = false) {
        if openAfterCheck {
            let instant = now()
            if let lastOpenAttempt, (0..<1).contains(instant - lastOpenAttempt) { return }
            lastOpenAttempt = instant
        }
        invalidate()
        guard storeBuild, strategy == "us-external", connected, canPresent() else { return }
        let request = generation
        let started = now()
        lookup = Task { @MainActor [weak self] in
            guard let self, !Task.isCancelled, request == self.generation,
                  self.connected, self.canPresent() else { return }
            let code = await self.country()
            guard !Task.isCancelled, request == self.generation, self.canPresent(),
                  (0...5).contains(self.now() - started) else { return }
            self.allowed = externalSupportAvailable(strategy: self.strategy, storeBuild: self.storeBuild,
                                                    countryCode: code, connected: self.connected)
            self.render(self.allowed)
            if openAfterCheck && self.allowed { self.open() }
        }
    }
}

func externalSupportSectionScript(visible: Bool) -> String {
    let flag = visible ? "true" : "false"
    return """
    (() => {
      document.getElementById('optional-coffee')?.remove();
      if (!\(flag)) return;
      const page = document.querySelector('.about-page');
      const footer = page?.querySelector('.about-footer');
      if (!page || !footer) return;
      const section = document.createElement('section');
      section.id = 'optional-coffee';
      section.className = 'support-section';
      section.setAttribute('aria-labelledby', 'coffee-heading');
      const copy = document.createElement('div');
      copy.className = 'support-copy';
      const heading = document.createElement('h2');
      heading.id = 'coffee-heading';
      heading.textContent = 'Support development';
      const invitation = document.createElement('p');
      invitation.textContent = 'Agent Board is free. If it helps you, you can buy me a coffee. This is optional and does not unlock features.';
      const privacy = document.createElement('p');
      privacy.textContent = 'Ko-fi handles any payment under its own terms and privacy policy, and may share your contact and transaction details with Spacely Digital. No board data is attached.';
      const link = document.createElement('a');
      link.className = 'coffee-link';
      link.href = 'https://ko-fi.com/spacelydigital';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Buy me a coffee';
      copy.append(heading, invitation, privacy, link);
      const qr = link.cloneNode(false);
      qr.className = 'qr-link';
      qr.setAttribute('aria-label', 'Open Spacely Digital’s Ko-fi page');
      const image = document.createElement('img');
      image.src = '/about/ko-fi.svg';
      image.width = 148; image.height = 148;
      image.alt = 'Scan to open ko-fi.com/spacelydigital';
      qr.append(image);
      section.append(copy, qr);
      page.insertBefore(section, footer);
    })();
    """
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, UNUserNotificationCenterDelegate, NSMenuItemValidation {
    var window: NSWindow!
    var webView: WKWebView!
    var retryTimer: Timer?
    var pollTimer: Timer?
    var statusItem: NSStatusItem!
    var statusMenu: NSMenu!
    var csrfToken: String?
    var chats: [AttentionChat] = []
    var polling = false
    var stateGeneration = UUID()
    var receivedInitialSnapshot = false
    var collectorAvailable = false
    var permission: UNAuthorizationStatus = .notDetermined
    var requestingPermission = false
    var notificationIntent = UUID()
    var lastDeliveryFailed = false
    var pendingNotifications: [String: AttentionDelivery] = [:]
    var notificationClock: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    var notificationToggleItems: [NSMenuItem] = []
    var notificationStatusItems: [NSMenuItem] = []
    var testNotificationItems: [NSMenuItem] = []
    var nextChatItems: [NSMenuItem] = []
    var setupMenuItem: NSMenuItem?
    var sampleBoardItem: NSMenuItem?
    var connectionItem: NSMenuItem!
    var seen: Set<String>
    var seenOrder: [String]
    var handledAt: [String: Double]
    let defaults: UserDefaults
    var boardURL: URL
    let fixtureMode: Bool
    let runtime = CollectorRuntime()
    var accessURLs: [URL] = []
    var sourceURLs: [String: URL] = [:]
    var startingCollector = false
    var authorizingClaude = false
    var sourceAccessChanged = false
    var credentialHelper: Process?
    var setupLogin: NSButton?
    var setupNotifications: NSButton?
    var collectorRetryButton: NSButton?
    var startupModeError: Error?
    let notificationPrefix: String
    let center = UNUserNotificationCenter.current()
    let session: URLSession
    var externalSupport: ExternalSupportController?
    var storefrontUpdates: Task<Void, Never>?
    var supportPath: NWPathMonitor?

    override init() {
        var selected = URL(string: "http://127.0.0.1:8769/")!
        var fixture = false
        #if AGENT_BOARD_DIRECT
        let arguments = ProcessInfo.processInfo.arguments
        if let index = arguments.firstIndex(of: "--board-url"), index + 1 < arguments.count,
           let candidate = URL(string: arguments[index + 1]), candidate.scheme == "http",
           candidate.host == "127.0.0.1", let port = candidate.port, (1024...65535).contains(port),
           candidate.path == "/", candidate.query == nil, candidate.fragment == nil,
           candidate.user == nil, candidate.password == nil {
            selected = candidate
            fixture = true
        }
        #endif
        boardURL = selected
        fixtureMode = fixture
        let suffix = fixture ? ".fixture.\(selected.port!)" : ""
        defaults = suffix.isEmpty ? .standard : UserDefaults(suiteName: "local.agentboard.display" + suffix)!
        notificationPrefix = "agent-board" + suffix
        seenOrder = []
        seen = []
        handledAt = [:]
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        configuration.timeoutIntervalForRequest = 4
        configuration.timeoutIntervalForResource = 6
        session = URLSession(configuration: configuration, delegate: LocalSessionDelegate(), delegateQueue: nil)
        super.init()
        restoreSelectedSampleMode()
        if startupModeError == nil { loadAttentionLedger() }
        let lastPort = defaults.integer(forKey: collectorPortKey)
        if (1024...65535).contains(lastPort) { runtime.preferredPort = lastPort }
    }

    let pageZoomLevels = [75, 100, 125, 150, 175, 200]
    var pageZoomPercent: Int {
        let saved = defaults.integer(forKey: "pageZoomPercentV1")
        return pageZoomLevels.contains(saved) ? saved : 100
    }

    var sampleModeSelected: Bool { runtime.sampleMode || startupModeError != nil }
    var seenLedgerKey: String { sampleModeSelected ? "sampleSeenAttentionEvents" : "seenAttentionEvents" }
    var handledLedgerKey: String { sampleModeSelected ? "sampleHandledAttentionEvents" : "handledAttentionEvents" }
    var displayPreferencesKey: String { sampleModeSelected ? "sampleDisplayPreferences" : "displayPreferences" }
    var collectorPortKey: String { sampleModeSelected ? "sampleLastCollectorPort" : "lastCollectorPort" }

    func restoreSelectedSampleMode() {
        guard runtime.storeDistribution, defaults.bool(forKey: "sampleBoardSelected") else { return }
        do { try runtime.setSampleMode(true); startupModeError = nil }
        catch { startupModeError = error }
    }

    func loadAttentionLedger() {
        seenOrder = Array((defaults.stringArray(forKey: seenLedgerKey) ?? []).suffix(3000))
        seen = Set(seenOrder)
        handledAt = defaults.dictionary(forKey: handledLedgerKey)?.reduce(into: [:]) { result, pair in
            if let value = pair.value as? NSNumber { result[pair.key] = value.doubleValue }
        } ?? [:]
    }

    var notificationsEnabled: Bool { defaults.bool(forKey: "notificationsEnabled") }
    func attentionNotificationSound() -> UNNotificationSound? {
        guard let name = AgentBoardAlertSound.filename(preferences: defaults.dictionary(forKey: displayPreferencesKey) ?? [:]) else { return nil }
        guard Bundle.main.url(forResource: name, withExtension: nil) != nil else { return .default }
        return UNNotificationSound(named: UNNotificationSoundName(rawValue: name))
    }
    var ownedCollectorRunning: Bool { fixtureMode || runtime.process?.isRunning == true }
    var permissionAllowsNotifications: Bool { permission == .authorized || permission == .provisional }
    var boardIsVisibleAndActive: Bool {
        NSApp.isActive && !NSApp.isHidden && window.isVisible && !window.isMiniaturized
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        center.delegate = self
        center.setNotificationCategories([
            UNNotificationCategory(identifier: "ATTENTION_OPEN", actions: [
                UNNotificationAction(identifier: "OPEN_CHAT", title: "Open Chat", options: []),
                UNNotificationAction(identifier: "SNOOZE", title: "Snooze for 10 Minutes", options: [])
            ], intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: "ATTENTION", actions: [
                UNNotificationAction(identifier: "SNOOZE", title: "Snooze for 10 Minutes", options: [])
            ], intentIdentifiers: [], options: [])
        ])
        buildMainMenu()
        buildStatusItem()

        let config = WKWebViewConfiguration()
        // The board and display preferences have explicit local stores. Avoid
        // retaining an additional disk-backed copy in WebKit's website data.
        config.websiteDataStore = .nonPersistent()
        config.preferences.tabFocusesLinks = true
        config.userContentController.add(self, name: "agentBoard")
        webView = WKWebView(frame: .zero, configuration: config)
        webView.pageZoom = CGFloat(pageZoomPercent) / 100
        webView.allowsLinkPreview = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Agent Board"
        window.delegate = self
        window.minSize = NSSize(width: 540, height: 440)
        window.contentView = webView
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("AgentBoardWindow")
        window.center()
        runtime.unexpectedExit = { [weak self] in
            self?.invalidateStateRequests()
            self?.collectorAvailable = false
            self?.retryTimer?.invalidate()
            self?.webView.stopLoading()
            self?.updateMenu()
            self?.presentStartupError(CollectorRuntime.RuntimeError.unavailable)
        }
        showBoard()
        refreshPermission()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            self?.poll()
        }
        RunLoop.main.add(pollTimer!, forMode: .common)
        if let error = startupModeError {
            DispatchQueue.main.async { [weak self] in self?.presentStartupError(error) }
        }
        else if fixtureMode { reload(); poll() }
        else if !runtime.sampleMode && !defaults.bool(forKey: "setupCompletedV1") {
            // Complete the Launch Services launch before opening a modal
            // consent dialog. No collector starts until Setup is accepted.
            DispatchQueue.main.async { [weak self] in self?.showSetup() }
        }
        else { startCollector() }
    }

    func item(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let value = NSMenuItem(title: title, action: action, keyEquivalent: key)
        value.target = self
        return value
    }

    func addNotificationItems(to menu: NSMenu) {
        let toggle = item("Enable Notifications", #selector(toggleNotifications))
        let status = NSMenuItem(title: "Notification permission has not been requested.", action: nil, keyEquivalent: "")
        status.isEnabled = false
        let test = item("Send Test Notification", #selector(sendTestNotification))
        menu.addItem(toggle)
        menu.addItem(status)
        menu.addItem(test)
        notificationToggleItems.append(toggle)
        notificationStatusItems.append(status)
        testNotificationItems.append(test)
    }

    func buildMainMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.autoenablesItems = false
        appMenu.addItem(item("About Agent Board", #selector(showAbout)))
        let setup = item("Setup and Permissions…", #selector(showSetup))
        setupMenuItem = setup
        appMenu.addItem(setup)
        if runtime.storeDistribution {
            let sample = item("Show Sample Board", #selector(toggleSampleBoard))
            sampleBoardItem = sample
            appMenu.addItem(sample)
        }
        appMenu.addItem(item("Open Local Data Folder…", #selector(openLocalData)))
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(item("Show Agent Board", #selector(showBoard)))
        let next = item("Open Next Chat", #selector(openNextChat))
        nextChatItems.append(next)
        appMenu.addItem(next)
        appMenu.addItem(NSMenuItem.separator())
        addNotificationItems(to: appMenu)
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Hide Agent Board", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Agent Board", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        menu.addItem(fileItem)
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Delete", action: #selector(NSText.delete(_:)), keyEquivalent: "")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        menu.addItem(editItem)
        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(item("Reload", #selector(reload), key: "r"))
        viewMenu.addItem(NSMenuItem.separator())
        for (title, key, step) in [("Zoom In", "+", 1), ("Zoom Out", "-", -1), ("Actual Size", "0", 0)] {
            let zoom = item(title, #selector(changePageZoom(_:)), key: key)
            zoom.tag = step
            viewMenu.addItem(zoom)
        }
        viewMenu.addItem(NSMenuItem.separator())
        viewMenu.addItem(item("Keep on Top", #selector(toggleOnTop(_:)), key: "t"))
        let fullscreen = viewMenu.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fullscreen.keyEquivalentModifierMask = [.control, .command]
        viewItem.submenu = viewMenu
        menu.addItem(viewItem)
        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = windowMenu
        menu.addItem(windowItem)
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = menu
    }

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "tray", accessibilityDescription: "Agent Board")
            button.imagePosition = .imageLeading
            button.title = " …"
            button.target = self
            button.action = #selector(statusItemClicked)
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        statusMenu = NSMenu()
        statusMenu.autoenablesItems = false
        statusMenu.addItem(item("Show Agent Board", #selector(showBoard)))
        let next = item("Open Next Chat", #selector(openNextChat))
        nextChatItems.append(next)
        statusMenu.addItem(next)
        connectionItem = NSMenuItem(title: "The local collector is connecting.", action: nil, keyEquivalent: "")
        connectionItem.isEnabled = false
        statusMenu.addItem(connectionItem)
        statusMenu.addItem(NSMenuItem.separator())
        addNotificationItems(to: statusMenu)
        statusMenu.addItem(NSMenuItem.separator())
        statusMenu.addItem(withTitle: "Quit Agent Board", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        updateMenu()
    }

    @objc func statusItemClicked() {
        if NSApp.currentEvent?.type == .rightMouseUp, let button = statusItem.button {
            statusMenu.popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.height + 4), in: button)
        } else { showBoard() }
    }

    func updateMenu() {
        setupMenuItem?.isEnabled = !startingCollector && !sampleModeSelected
        sampleBoardItem?.title = sampleModeSelected ? "Return to Live Board" : "Show Sample Board"
        sampleBoardItem?.isEnabled = !startingCollector
        let eligible = chats.filter { $0.eligible }
        let count = eligible.count
        if receivedInitialSnapshot {
            statusItem?.button?.title = " \(count)" + (collectorAvailable ? "" : "?")
        }
        statusItem?.button?.toolTip = collectorAvailable
            ? "\(count) conversation\(count == 1 ? "" : "s") need\(count == 1 ? "s" : "") review. Click to show Agent Board; right-click for options."
            : "The local collector is unavailable. The count is the last known value."
        let reviewCount = "\(count) conversation\(count == 1 ? "" : "s") need\(count == 1 ? "s" : "") review."
        statusItem?.button?.setAccessibilityLabel(collectorAvailable
            ? "Agent Board: \(reviewCount)"
            : receivedInitialSnapshot
                ? "Agent Board: the local collector is unavailable. Last known: \(reviewCount)"
                : "Agent Board: connecting to the local collector.")
        connectionItem?.isHidden = collectorAvailable
        connectionItem?.title = receivedInitialSnapshot ? "The collector is unavailable; the count is the last known value." : "The local collector is connecting."
        for next in nextChatItems { next.isEnabled = collectorAvailable && eligible.contains { $0.canOpen } }
        for toggle in notificationToggleItems {
            toggle.state = notificationsEnabled && permissionAllowsNotifications ? .on : .off
            toggle.isEnabled = !requestingPermission && !sampleModeSelected
        }
        let status: String
        if sampleModeSelected { status = "Notifications are unavailable in the sample board." }
        else if lastDeliveryFailed { status = "The last notification could not be delivered." }
        else if requestingPermission { status = "Notification permission is being requested." }
        else if permission == .denied { status = "Notifications are blocked in System Settings." }
        else if permissionAllowsNotifications { status = notificationsEnabled ? "Notifications are enabled." : "Notifications are turned off." }
        else { status = "Notification permission has not been requested." }
        for label in notificationStatusItems { label.title = status }
        for test in testNotificationItems { test.isEnabled = !sampleModeSelected && notificationsEnabled && permissionAllowsNotifications }
    }

    func refreshPermission() {
        center.getNotificationSettings { [weak self] settings in
            DispatchQueue.main.async {
                self?.permission = settings.authorizationStatus
                self?.updateMenu()
            }
        }
    }

    @objc func toggleNotifications() {
        let intent = UUID()
        notificationIntent = intent
        if notificationsEnabled && permissionAllowsNotifications {
            disableNotifications()
            return
        }
        requestingPermission = true
        updateMenu()
        center.getNotificationSettings { [weak self] settings in
            DispatchQueue.main.async {
                guard let self, self.notificationIntent == intent else { return }
                if settings.authorizationStatus == .denied {
                    self.requestingPermission = false
                    self.permission = .denied
                    self.defaults.set(false, forKey: "notificationsEnabled")
                    self.updateMenu()
                    let alert = NSAlert()
                    alert.messageText = "Notifications are blocked."
                    alert.informativeText = "Allow Agent Board in System Settings under Notifications, then enable notifications here."
                    alert.addButton(withTitle: "Open Settings")
                    alert.addButton(withTitle: "Cancel")
                    if alert.runModal() == .alertFirstButtonReturn,
                       let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
                        NSWorkspace.shared.open(url)
                    }
                    return
                }
                self.center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
                    DispatchQueue.main.async {
                        guard self.notificationIntent == intent else { return }
                        self.requestingPermission = false
                        self.defaults.set(granted, forKey: "notificationsEnabled")
                        self.lastDeliveryFailed = false
                        self.refreshPermission()
                    }
                }
            }
        }
    }

    func disableNotifications() {
        notificationIntent = UUID()
        requestingPermission = false
        defaults.set(false, forKey: "notificationsEnabled")
        updateMenu()
    }

    @objc func sendTestNotification() {
        guard notificationsEnabled && permissionAllowsNotifications else { return }
        let content = UNMutableNotificationContent()
        content.title = "Agent Board"
        content.body = "Notifications are enabled for conversations that need your attention."
        content.sound = attentionNotificationSound()
        content.userInfo = ["test": true]
        center.add(UNNotificationRequest(identifier: notificationPrefix + ".test", content: content, trigger: nil)) { [weak self] error in
            DispatchQueue.main.async { self?.lastDeliveryFailed = error != nil; self?.updateMenu() }
        }
    }

    func fetchState(_ completion: @escaping (Result<[String: Any], Error>) -> Void) {
        let generation = stateGeneration
        var request = URLRequest(url: boardURL.appendingPathComponent("api/state"))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        #if AGENT_BOARD_DIRECT
        if !fixtureMode { request.setValue(runtime.accessToken, forHTTPHeaderField: "X-Agent-Board-Access") }
        #else
        request.setValue(runtime.accessToken, forHTTPHeaderField: "X-Agent-Board-Access")
        #endif
        session.dataTask(with: request) { [weak self] data, response, error in
            var result: Result<[String: Any], Error>
            if let error { result = .failure(error) }
            else if let http = response as? HTTPURLResponse, http.statusCode == 200,
                    let data, let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                    value["sessions"] is [[String: Any]], value["csrf_token"] is String {
                result = .success(value)
            } else { result = .failure(NSError(domain: "AgentBoard", code: 1)) }
            DispatchQueue.main.async {
                guard let self, self.stateGeneration == generation else { return }
                completion(result)
            }
        }.resume()
    }

    func poll() {
        guard !polling, !startingCollector, ownedCollectorRunning,
              fixtureMode || runtime.sampleMode || defaults.bool(forKey: "setupCompletedV1") else { return }
        polling = true
        let generation = stateGeneration
        fetchState { [weak self] result in
            guard let self, self.stateGeneration == generation else { return }
            self.polling = false
            switch result {
            case .success(let state): self.applySnapshot(state)
            case .failure: self.collectorAvailable = false; self.updateMenu()
            }
        }
    }

    func applySnapshot(_ state: [String: Any]) {
        csrfToken = state["csrf_token"] as? String
        let now = (state["now"] as? NSNumber)?.doubleValue ?? Date().timeIntervalSince1970
        let lastScan = (state["last_scan"] as? NSNumber)?.doubleValue ?? now
        collectorAvailable = now - lastScan < 15
        let incoming = (state["sessions"] as? [[String: Any]] ?? []).compactMap { AttentionChat($0, now: now, sampleMode: runtime.sampleMode) }
        guard collectorAvailable else {
            // Retain the last known count without consuming attention events
            // that must still be considered when the collector recovers.
            chats = incoming
            updateMenu()
            return
        }
        let deliveryNow = notificationClock()
        let canNotify = !runtime.sampleMode && notificationsEnabled && permissionAllowsNotifications && !boardIsVisibleAndActive
        let eligibleDeliveries = incoming.reduce(into: [String: Double]()) { result, chat in
            if chat.eligible && (!chat.reconfirmationSilent || chat.snoozedUntil > 0) {
                result[chat.signature] = chat.snoozedUntil
            }
        }
        // Retries belong to this exact attention event and reminder. They never
        // survive review, replacement, revoked access or a different board.
        pendingNotifications = pendingNotifications.filter { signature, delivery in
            canNotify && delivery.generation == stateGeneration && delivery.intent == notificationIntent
                && delivery.expiresAt > deliveryNow && eligibleDeliveries[signature] == delivery.snoozedUntil
                && delivery.snapshotExpiresAt > now
        }
        var ledgerChanged = false
        for chat in incoming where !chat.eventID.isEmpty {
            let known = seen.contains(chat.signature)
            let reminderDue = known && chat.snoozedUntil > 0 && chat.snoozedUntil <= now
                && (handledAt[chat.signature] ?? 0) < chat.snoozedUntil
            let newEvent = receivedInitialSnapshot && !known
            if !known {
                seen.insert(chat.signature)
                seenOrder.append(chat.signature)
                handledAt[chat.signature] = now
                ledgerChanged = true
            }
            guard chat.eligible && ((newEvent && !chat.reconfirmationSilent) || reminderDue) else { continue }
            // Viewing the board or turning notifications off handles this event
            // without creating an alert later merely because the window closes.
            handledAt[chat.signature] = now
            ledgerChanged = true
            if canNotify && pendingNotifications.count < 3000 {
                pendingNotifications[chat.signature] = AttentionDelivery(generation: stateGeneration,
                    intent: notificationIntent, snoozedUntil: chat.snoozedUntil,
                    expiresAt: deliveryNow + 120, snapshotExpiresAt: now + 120, retryAt: deliveryNow)
            }
        }
        if seenOrder.count > 3000 {
            seenOrder = Array(seenOrder.suffix(3000))
            seen = Set(seenOrder)
            handledAt = handledAt.filter { seen.contains($0.key) }
            ledgerChanged = true
        }
        if ledgerChanged {
            defaults.set(seenOrder, forKey: seenLedgerKey)
            defaults.set(handledAt, forKey: handledLedgerKey)
        }
        for chat in incoming where chat.eligible {
            if let delivery = pendingNotifications[chat.signature], !delivery.inFlight, delivery.retryAt <= deliveryNow {
                notify(chat)
            }
        }
        chats = incoming
        receivedInitialSnapshot = true
        updateMenu()
    }

    func notify(_ chat: AttentionChat) {
        guard var delivery = pendingNotifications[chat.signature], !delivery.inFlight,
              delivery.generation == stateGeneration, delivery.intent == notificationIntent else { return }
        delivery.attempts += 1
        delivery.attempt = UUID()
        delivery.inFlight = true
        pendingNotifications[chat.signature] = delivery
        let attempt = delivery.attempt
        let generation = delivery.generation
        let content = UNMutableNotificationContent()
        content.title = chat.title
        content.body = chat.status == "needs_you" ? "\(chat.provider) needs your approval or input." : "\(chat.provider) has a reply ready to review."
        content.sound = attentionNotificationSound()
        content.categoryIdentifier = chat.canOpen ? "ATTENTION_OPEN" : "ATTENTION"
        content.userInfo = ["key": chat.key, "event_id": chat.eventID, "can_open": chat.canOpen]
        let digest = SHA256.hash(data: Data(chat.signature.utf8)).map { String(format: "%02x", $0) }.joined()
        let identifier = notificationPrefix + "." + digest
        center.add(UNNotificationRequest(identifier: identifier, content: content, trigger: nil)) { [weak self] error in
            DispatchQueue.main.async {
                guard let self, self.stateGeneration == generation,
                      var pending = self.pendingNotifications[chat.signature], pending.attempt == attempt,
                      pending.intent == self.notificationIntent else { return }
                self.lastDeliveryFailed = error != nil
                if error != nil && pending.attempts < 3 && pending.expiresAt > self.notificationClock() {
                    pending.inFlight = false
                    pending.retryAt = self.notificationClock() + (pending.attempts == 1 ? 5 : 15)
                    self.pendingNotifications[chat.signature] = pending
                } else {
                    // A nil completion means the OS accepted the request. Never
                    // retry it based on whether a banner happened to be visible.
                    self.pendingNotifications.removeValue(forKey: chat.signature)
                }
                self.updateMenu()
            }
        }
    }

    func performAction(_ action: String, key: String, eventID: String, retry: Bool = true) {
        guard ownedCollectorRunning else {
            showActionError("The local collector is unavailable. Retry to resume monitoring.")
            return
        }
        let generation = stateGeneration
        guard let token = csrfToken else {
            fetchState { [weak self] result in
                if case .success(let state) = result {
                    self?.applySnapshot(state)
                    self?.performAction(action, key: key, eventID: eventID, retry: false)
                } else { self?.showActionError("The local collector is unavailable. Try again when it reconnects.") }
            }
            return
        }
        var request = URLRequest(url: boardURL.appendingPathComponent("api").appendingPathComponent("sessions").appendingPathComponent(key).appendingPathComponent(action))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(token, forHTTPHeaderField: "X-Agent-Board-Token")
        #if AGENT_BOARD_DIRECT
        if !fixtureMode { request.setValue(runtime.accessToken, forHTTPHeaderField: "X-Agent-Board-Access") }
        #else
        request.setValue(runtime.accessToken, forHTTPHeaderField: "X-Agent-Board-Access")
        #endif
        var body: [String: Any] = ["event_id": eventID]
        if action == "snooze" { body["minutes"] = 10 }
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        session.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self, self.stateGeneration == generation else { return }
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                if status == 403 && retry {
                    self.csrfToken = nil
                    self.performAction(action, key: key, eventID: eventID, retry: false)
                } else if error == nil && (200...299).contains(status) {
                    if action == "open", self.runtime.sandboxed {
                        let value = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                        if let link = value?["native_open_url"] as? String, let provider = value?["provider"] as? String {
                            self.openExistingChat(link, provider: provider)
                        } else { self.showActionError("The conversation link is unavailable. Open the provider app directly.") }
                    }
                    self.poll()
                }
                else {
                    let value = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                    self.showActionError(value?["error"] as? String ?? "The action could not be completed. Open Agent Board and try again.")
                }
            }
        }.resume()
    }

    @objc func openNextChat() {
        guard let chat = chats.first(where: { $0.eligible && $0.canOpen }) else { return }
        performAction("open", key: chat.key, eventID: chat.eventID)
    }

    func openExistingChat(_ value: String, provider: String) {
        guard !sampleModeSelected else {
            showActionError("Sample chats do not open provider apps.")
            return
        }
        guard let url = existingChatURL(value, provider: provider) else {
            showActionError("The conversation link is invalid.")
            return
        }
        let bundleID = provider == "codex" ? "com.openai.codex" : "com.anthropic.claudefordesktop"
        guard let application = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else {
            showActionError("The provider app is not installed. Install it to open this conversation.")
            return
        }
        NSWorkspace.shared.open([url], withApplicationAt: application, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
            if error != nil {
                DispatchQueue.main.async { self?.showActionError("The provider app could not open this conversation.") }
            }
        }
    }

    func showActionError(_ message: String) {
        showBoard()
        let alert = NSAlert()
        alert.messageText = "The conversation action could not be completed."
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        DispatchQueue.main.async { [weak self] in
            let test = notification.request.content.userInfo["test"] as? Bool == true
            completionHandler(test || self?.boardIsVisibleAndActive == false ? [.banner, .sound] : [])
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        DispatchQueue.main.async { [weak self] in
            guard let self else { completionHandler(); return }
            guard !self.sampleModeSelected else { self.showBoard(); completionHandler(); return }
            if let key = info["key"] as? String, let eventID = info["event_id"] as? String {
                if response.actionIdentifier == "SNOOZE" {
                    self.performAction("snooze", key: key, eventID: eventID)
                } else if response.actionIdentifier == "OPEN_CHAT" || response.actionIdentifier == UNNotificationDefaultActionIdentifier {
                    if info["can_open"] as? Bool == true { self.performAction("open", key: key, eventID: eventID) }
                    else { self.showBoard() }
                }
                center.removeDeliveredNotifications(withIdentifiers: [response.notification.request.identifier])
            } else if response.actionIdentifier == UNNotificationDefaultActionIdentifier { self.showBoard() }
            completionHandler()
        }
    }

    @objc func showBoard() {
        if window == nil { return }
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc func reload() {
        guard !startingCollector, ownedCollectorRunning else { return }
        webView.load(URLRequest(url: boardURL))
    }
    @objc func changePageZoom(_ sender: NSMenuItem) {
        guard [-1, 0, 1].contains(sender.tag) else { return }
        let index = pageZoomLevels.firstIndex(of: pageZoomPercent) ?? 1
        let next = sender.tag == 0 ? 100 : pageZoomLevels[min(max(index + sender.tag, 0), pageZoomLevels.count - 1)]
        webView.pageZoom = CGFloat(next) / 100
        defaults.set(next, forKey: "pageZoomPercentV1")
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        guard menuItem.action == #selector(changePageZoom(_:)) else { return true }
        if menuItem.tag < 0 { return pageZoomPercent > pageZoomLevels[0] }
        if menuItem.tag > 0 { return pageZoomPercent < pageZoomLevels[pageZoomLevels.count - 1] }
        return pageZoomPercent != 100
    }

    @objc func toggleFullscreen() { window.toggleFullScreen(nil) }

    func publishFullscreenState() {
        guard !startingCollector, let url = webView.url,
              url.scheme == boardURL.scheme, url.host == boardURL.host, url.port == boardURL.port,
              url.path == "/" || url.path == "/index.html" else { return }
        let active = window.styleMask.contains(.fullScreen) ? "true" : "false"
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('agentboard:fullscreenchange', { detail: \(active) }));", completionHandler: nil)
    }

    func windowDidEnterFullScreen(_ notification: Notification) { publishFullscreenState() }
    func windowDidExitFullScreen(_ notification: Notification) { publishFullscreenState() }
    @objc func toggleOnTop(_ sender: NSMenuItem) {
        let enabled = window.level != .floating
        window.level = enabled ? .floating : .normal
        sender.state = enabled ? .on : .off
    }

    @objc func showSetup() {
        guard !startingCollector, !sampleModeSelected else { return }
        showBoard()
        let alert = NSAlert()
        alert.messageText = "Set up Agent Board"
        alert.addButton(withTitle: defaults.bool(forKey: "setupCompletedV1") ? "Save" : "Start Monitoring")
        alert.addButton(withTitle: "Cancel")
        if runtime.storeDistribution && !defaults.bool(forKey: "setupCompletedV1") {
            alert.addButton(withTitle: "Try Sample Board")
        }
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 10
        let explanation = NSTextField(wrappingLabelWithString: runtime.sandboxed
            ? "Agent Board reads only the source folders you select below. Your board and preferences stay on this Mac. Codex usage uses local readings, without reading saved sign-ins or contacting OpenAI or Anthropic."
            : "Agent Board reads local Codex and Claude Code sessions to build your review queue. Your board and preferences stay on this Mac.\n\nOptional account features read usage, chat titles and reply status directly from OpenAI or Anthropic using your existing sign-in. Regular chat monitoring includes web and desktop conversations in the signed-in account. A ChatGPT latest-reply check can include message text; Agent Board keeps only reply identifiers, timestamps and completion status, then discards the response. If Claude account access is unavailable, use Allow Claude Account Access below. Background reads do not show Keychain prompts.")
        explanation.preferredMaxLayoutWidth = 440
        explanation.widthAnchor.constraint(equalToConstant: 440).isActive = true
        content.addArrangedSubview(explanation)
        let notifications = NSButton(checkboxWithTitle: "Enable notifications when a chat needs review", target: nil, action: nil)
        notifications.state = notificationsEnabled ? .on : .off
        content.addArrangedSubview(notifications)
        setupNotifications = notifications
        let usage = NSButton(checkboxWithTitle: "Read account usage from OpenAI and Anthropic", target: nil, action: nil)
        usage.state = defaults.bool(forKey: "accountUsageEnabled") ? .on : .off
        if runtime.sandboxed { usage.state = .off; usage.isEnabled = false }
        if !runtime.sandboxed { content.addArrangedSubview(usage) }
        let projects = NSButton(checkboxWithTitle: "Read Claude project status from Anthropic", target: nil, action: nil)
        projects.state = defaults.bool(forKey: "cloudProjectsEnabled") ? .on : .off
        if runtime.sandboxed { projects.state = .off; projects.isEnabled = false }
        if !runtime.sandboxed { content.addArrangedSubview(projects) }
        let ignoreClaudeChats = NSButton(checkboxWithTitle: "Ignore regular Claude chats", target: nil, action: nil)
        let ignoreChatGPTChats = NSButton(checkboxWithTitle: "Ignore regular ChatGPT chats", target: nil, action: nil)
        let supportsAccountChats = !runtime.sandboxed && !runtime.storeDistribution && !fixtureMode
        if supportsAccountChats {
            ignoreClaudeChats.state = defaults.bool(forKey: "claudeDesktopChatsEnabled") ? .off : .on
            ignoreChatGPTChats.state = defaults.bool(forKey: "chatGPTDesktopChatsEnabled") ? .off : .on
            ignoreClaudeChats.toolTip = "Uncheck to monitor web and desktop chats for the account signed in to Claude on this Mac."
            ignoreChatGPTChats.toolTip = "Uncheck to monitor web and desktop chats for your existing OpenAI account on this Mac."
            content.addArrangedSubview(ignoreClaudeChats)
            content.addArrangedSubview(ignoreChatGPTChats)
        }
        if !runtime.sandboxed {
            content.addArrangedSubview(NSButton(title: "Allow Claude Account Access…", target: self, action: #selector(authorizeClaude)))
        }
        let login = NSButton(checkboxWithTitle: "Open Agent Board at login", target: nil, action: nil)
        if #available(macOS 13.0, *) {
            login.state = [.enabled, .requiresApproval].contains(SMAppService.mainApp.status) ? .on : .off
        } else { login.isEnabled = false; login.toolTip = "Opening at login requires macOS 13 or later." }
        content.addArrangedSubview(login)
        setupLogin = login
        let permissions = NSTextField(wrappingLabelWithString: runtime.sandboxed
            ? "Select Codex’s folder and both Claude folders to monitor those apps. Sources remain disconnected until their folders are selected. Folder access is read only. Full Disk Access and Accessibility are not required."
            : "If a source reports an access error, grant access to its folder below, then retry. Full Disk Access and Accessibility are not required.")
        permissions.preferredMaxLayoutWidth = 440
        permissions.widthAnchor.constraint(equalToConstant: 440).isActive = true
        content.addArrangedSubview(permissions)
        let folders = NSStackView()
        folders.orientation = .horizontal
        folders.spacing = 8
        folders.addArrangedSubview(NSButton(title: "Codex Folder…", target: self, action: #selector(grantCodexFolder)))
        folders.addArrangedSubview(NSButton(title: "Claude Sessions…", target: self, action: #selector(grantClaudeSessions)))
        folders.addArrangedSubview(NSButton(title: "Claude Code in Desktop…", target: self, action: #selector(grantClaudeFolder)))
        content.addArrangedSubview(folders)
        if claudeDesktopNeedsReselection {
            let migration = NSTextField(wrappingLabelWithString: "Choose the Claude Code sessions folder. The previous Claude app folder permission is no longer used.")
            migration.preferredMaxLayoutWidth = 440
            content.addArrangedSubview(migration)
        }
        #if AGENT_BOARD_DIRECT
        if runtime.sandboxed && !runtime.storeDistribution {
            content.addArrangedSubview(NSButton(title: "Connect Claude Usage…", target: self, action: #selector(connectClaudeUsage)))
        }
        #endif
        content.addArrangedSubview(NSButton(title: "Forget Source Folders", target: self, action: #selector(forgetSourceFolders)))
        let documentHeight = ceil(content.fittingSize.height)
        content.frame = NSRect(x: 0, y: 0, width: 440, height: documentHeight)
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 460, height: 1))
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .legacy
        scroll.drawsBackground = false
        scroll.documentView = content
        alert.accessoryView = scroll
        alert.layout()
        let available = (window.screen ?? NSScreen.main)?.visibleFrame.insetBy(dx: 16, dy: 16)
        let fixedHeight = alert.window.frame.height - scroll.frame.height
        let accessoryHeight = min(documentHeight, max(1, (available?.height ?? 700) - fixedHeight))
        scroll.setFrameSize(NSSize(width: 460, height: accessoryHeight))
        alert.layout()
        if let available {
            alert.window.setFrameOrigin(NSPoint(x: available.midX - alert.window.frame.width / 2,
                                                y: available.midY - alert.window.frame.height / 2))
        }
        let top = content.isFlipped ? 0 : max(0, documentHeight - scroll.contentSize.height)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: top))
        scroll.reflectScrolledClipView(scroll.contentView)
        let response = alert.runModal()
        if runtime.storeDistribution && !defaults.bool(forKey: "setupCompletedV1") && response == .alertThirdButtonReturn {
            setBoardMode(sample: true)
            return
        }
        guard response == .alertFirstButtonReturn else {
            if !defaults.bool(forKey: "setupCompletedV1") { showSetupPendingPage() }
            return
        }
        if !fixtureMode && runtime.needsLegacyMigration {
            let migration = NSAlert()
            migration.messageText = "Replace the previous background collector?"
            migration.informativeText = "This Mac has an older Agent Board login service. Agent Board can stop and disable that service, then use the collector included in this app. Your saved board, names, and settings will be kept. A copy of the old service configuration will be saved locally."
            migration.addButton(withTitle: "Replace Previous Collector")
            migration.addButton(withTitle: "Cancel")
            guard migration.runModal() == .alertFirstButtonReturn else {
                showSetupPendingPage()
                return
            }
            do { try runtime.migrateLegacyService() }
            catch { presentStartupError(error); return }
        }
        do {
            if #available(macOS 13.0, *), !fixtureMode {
                let status = try updateLoginPreference(enabled: login.state == .on, service: SMAppService.mainApp,
                                                       onFailure: { NSLog("%@", $0) })
                if login.state == .on && status == .requiresApproval {
                    let approval = NSAlert()
                    approval.messageText = "Opening at login needs approval."
                    approval.informativeText = "Allow Agent Board under Login Items in System Settings. Your board can continue running normally while this is off."
                    approval.addButton(withTitle: "Open Settings")
                    approval.addButton(withTitle: "Later")
                    if approval.runModal() == .alertFirstButtonReturn { SMAppService.openSystemSettingsLoginItems() }
                }
            }
        } catch {
            let failure = NSAlert()
            failure.messageText = "The login preference could not be saved."
            failure.informativeText = (error as? LoginPreferenceError)?.localizedDescription
                ?? "Agent Board can still run normally. You can review Login Items in System Settings or try this preference again later."
            failure.addButton(withTitle: "Continue")
            failure.runModal()
        }
        let networkChanged = defaults.bool(forKey: "accountUsageEnabled") != (usage.state == .on)
            || defaults.bool(forKey: "cloudProjectsEnabled") != (projects.state == .on)
        let claudeChatsEnabled = supportsAccountChats && ignoreClaudeChats.state == .off
        let chatGPTChatsEnabled = supportsAccountChats && ignoreChatGPTChats.state == .off
        let chatSourceChanged = supportsAccountChats && (
            defaults.bool(forKey: "claudeDesktopChatsEnabled") != claudeChatsEnabled
            || defaults.bool(forKey: "chatGPTDesktopChatsEnabled") != chatGPTChatsEnabled
        )
        defaults.set(usage.state == .on, forKey: "accountUsageEnabled")
        defaults.set(projects.state == .on, forKey: "cloudProjectsEnabled")
        if supportsAccountChats {
            defaults.set(claudeChatsEnabled, forKey: "claudeDesktopChatsEnabled")
            defaults.set(chatGPTChatsEnabled, forKey: "chatGPTDesktopChatsEnabled")
        }
        defaults.set(true, forKey: "setupCompletedV1")
        if notifications.state == .on && !(notificationsEnabled && permissionAllowsNotifications) { toggleNotifications() }
        else if notifications.state == .off { disableNotifications() }
        if fixtureMode { reload(); poll() }
        else if networkChanged || chatSourceChanged || sourceAccessChanged || !collectorAvailable { startCollector() }
    }

    @objc func grantCodexFolder() { grantFolder(name: "Codex", relative: ".codex", key: "codexSourceBookmark") }
    @objc func grantClaudeFolder() { grantFolder(name: "Claude Code sessions", relative: "Library/Application Support/Claude/claude-code-sessions", key: "claudeDesktopSessionsBookmarkV2") }
    @objc func grantClaudeSessions() { grantFolder(name: "Claude Sessions", relative: ".claude", key: "claudeSessionsBookmark") }

    #if AGENT_BOARD_DIRECT
    @objc func connectClaudeUsage() {
        guard !sampleModeSelected else { return }
        let directory = runtime.executable.deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/integrations/claude-local-usage", isDirectory: true)
        guard FileManager.default.fileExists(atPath: directory.appendingPathComponent(".claude-plugin/plugin.json").path),
              FileManager.default.fileExists(atPath: directory.appendingPathComponent("hooks/register.js").path) else {
            showActionError("The Claude usage exporter is missing from this copy of Agent Board.")
            return
        }
        let alert = NSAlert()
        alert.messageText = "Connect local Claude usage"
        alert.informativeText = "The optional Claude plugin exports cached five-hour and weekly quota readings on this Mac. It does not export chats or sign-in details or make network requests.\n\nUse Show Plugin Files to reveal claude-local-usage. In Finder, copy that entire folder into ~/.claude/skills/. Stop if a folder with that name already exists; do not replace or merge it. Then start a new local Code session in Claude Desktop. This requires embedded Claude Code 2.1.299 or later. Review any trust request before enabling the plugin.\n\nKeep the Claude Sessions folder selected here. Readings may remain unavailable until Claude receives an ordinary response. Agent Board never sends a prompt to refresh them."
        alert.addButton(withTitle: "Copy Plugin Path")
        alert.addButton(withTitle: "Show Plugin Files")
        alert.addButton(withTitle: "Cancel")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(directory.path, forType: .string)
        case .alertSecondButtonReturn:
            NSWorkspace.shared.activateFileViewerSelecting([directory])
        default: break
        }
    }
    #endif

    private var sourceBookmarkKeys: [(key: String, provider: String)] {
        [("codexSourceBookmark", "codex"), ("claudeSessionsBookmark", "claude"), ("claudeDesktopSessionsBookmarkV2", "claude_desktop")]
    }

    private var claudeDesktopNeedsReselection: Bool {
        defaults.data(forKey: "claudeSourceBookmark") != nil
            && defaults.data(forKey: "claudeDesktopSessionsBookmarkV2") == nil
    }

    private func validSourceGrant(_ url: URL, provider: String) -> Bool {
        guard provider == "claude_desktop" else { return true }
        let expected = sourceHome.appendingPathComponent("Library/Application Support/Claude/claude-code-sessions").standardizedFileURL
        return url.standardizedFileURL == expected
            && url.resolvingSymlinksInPath().lastPathComponent == "claude-code-sessions"
    }

    private var sourceHome: URL {
        // NSHomeDirectory points into the app container in a sandbox. The
        // account record provides only the picker’s suggested source path.
        if runtime.sandboxed, let user = getpwuid(getuid()), let directory = user.pointee.pw_dir {
            return URL(fileURLWithPath: String(cString: directory), isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
    }

    func grantFolder(name: String, relative: String, key: String) {
        guard !sampleModeSelected else { return }
        let expected = sourceHome.appendingPathComponent(relative).standardizedFileURL
        let panel = NSOpenPanel()
        panel.title = "Allow access to the \(name) folder"
        panel.message = "Choose this folder:\n\(expected.path)\n\nAccess is read-only. Agent Board reads\nsession metadata and previews."
        panel.prompt = "Allow Folder Access"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.showsHiddenFiles = true
        panel.directoryURL = expected
        guard panel.runModal() == .OK, let selected = panel.url else { return }
        let provider = sourceBookmarkKeys.first(where: { $0.key == key })?.provider
        guard selected.standardizedFileURL == expected,
              provider.map({ validSourceGrant(selected, provider: $0) }) ?? false else {
            let alert = NSAlert()
            alert.messageText = "Choose the \(name) source folder."
            alert.informativeText = "The folder must be \(expected.path). If \(name) is not installed or signed in, you can continue without it."
            alert.runModal()
            return
        }
        do {
            let bookmark = try selected.bookmarkData(options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess], includingResourceValuesForKeys: nil, relativeTo: nil)
            defaults.set(bookmark, forKey: key)
            if key == "claudeDesktopSessionsBookmarkV2" { defaults.removeObject(forKey: "claudeSourceBookmark") }
            if let provider {
                if let previous = sourceURLs[provider] {
                    previous.stopAccessingSecurityScopedResource()
                    accessURLs.removeAll { $0 == previous }
                }
                if validSourceGrant(selected, provider: provider) && selected.startAccessingSecurityScopedResource() {
                    accessURLs.append(selected)
                    sourceURLs[provider] = selected
                }
            }
            sourceAccessChanged = true
        } catch {
            let alert = NSAlert()
            alert.messageText = "Folder access could not be saved."
            alert.informativeText = "You can continue with the available sources and try granting this folder again later."
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }
    }

    func restoreSourceAccess() {
        guard !sampleModeSelected else { return }
        for (key, provider) in sourceBookmarkKeys {
            guard let bookmark = defaults.data(forKey: key) else { continue }
            if sourceURLs[provider] != nil { continue }
            var stale = false
            if let url = try? URL(resolvingBookmarkData: bookmark, options: [.withSecurityScope, .withoutUI, .withoutMounting], relativeTo: nil, bookmarkDataIsStale: &stale),
               validSourceGrant(url, provider: provider), !accessURLs.contains(url), url.startAccessingSecurityScopedResource() {
                accessURLs.append(url)
                sourceURLs[provider] = url
                if stale, let updated = try? url.bookmarkData(options: [.withSecurityScope, .securityScopeAllowOnlyReadAccess], includingResourceValuesForKeys: nil, relativeTo: nil) {
                    defaults.set(updated, forKey: key)
                }
            }
        }
    }

    @objc func forgetSourceFolders() {
        // Forget is an explicit action independent of the Setup form's draft
        // checkboxes. Stop the child before releasing its old grants.
        invalidateStateRequests()
        runtime.stop()
        collectorAvailable = false
        for url in accessURLs { url.stopAccessingSecurityScopedResource() }
        accessURLs.removeAll()
        sourceURLs.removeAll()
        runtime.grantedSourceURLs.removeAll()
        for (key, _) in sourceBookmarkKeys { defaults.removeObject(forKey: key) }
        defaults.removeObject(forKey: "claudeSourceBookmark")
        sourceAccessChanged = true
        if !fixtureMode && defaults.bool(forKey: "setupCompletedV1") { startCollector() }
        else { updateMenu() }
    }

    func startCollector() {
        guard !fixtureMode, !startingCollector else { return }
        if startupModeError != nil {
            restoreSelectedSampleMode()
            if let error = startupModeError { presentStartupError(error); return }
            loadAttentionLedger()
        }
        invalidateStateRequests()
        startingCollector = true
        if !runtime.sampleMode {
            runtime.recordStartupPhase(.restoringSources)
            restoreSourceAccess()
            runtime.recordStartupPhase(.sourcesRestored)
        }
        runtime.grantedSourceURLs = runtime.sampleMode ? [:] : sourceURLs
        sourceAccessChanged = false
        runtime.usageEnabled = !runtime.sandboxed && defaults.bool(forKey: "accountUsageEnabled")
        runtime.projectsEnabled = !runtime.sandboxed && defaults.bool(forKey: "cloudProjectsEnabled")
        runtime.claudeChatsEnabled = !runtime.sandboxed && !runtime.storeDistribution && defaults.bool(forKey: "claudeDesktopChatsEnabled")
        runtime.chatGPTChatsEnabled = !runtime.sandboxed && !runtime.storeDistribution && defaults.bool(forKey: "chatGPTDesktopChatsEnabled")
        csrfToken = nil
        collectorAvailable = false
        updateMenu()
        window.title = runtime.sampleMode ? "Agent Board: Sample Board" : "Agent Board"
        window.contentView = webView
        collectorRetryButton = nil
        window.makeFirstResponder(webView)
        webView.loadHTMLString("<!doctype html><html><meta name='viewport' content='width=device-width'><body style='font:16px -apple-system;padding:48px;color:#777'><h1 style='font-size:24px'>Agent Board is starting.</h1><p>Your saved board will appear when the local collector is ready.</p></body></html>", baseURL: nil)
        runtime.startupWaiting = { [weak self] in
            guard let self, self.startingCollector else { return }
            self.webView.loadHTMLString("<!doctype html><html><meta name='viewport' content='width=device-width'><body style='font:16px -apple-system;padding:48px;color:#777'><h1 style='font-size:24px'>Agent Board is still starting.</h1><p>Check for a macOS permission request and allow the folders you selected if you want monitoring to continue. The local collector is still waiting, and your saved board has not been removed.</p></body></html>", baseURL: nil)
        }
        runtime.start { [weak self] result in
            guard let self else { return }
            self.startingCollector = false
            switch result {
            case .success(let url):
                self.boardURL = url
                self.defaults.set(url.port, forKey: self.collectorPortKey)
                self.installAccessScript()
                self.receivedInitialSnapshot = false
                self.reload()
                self.poll()
            case .failure(let error): self.presentStartupError(error)
            }
        }
    }

    func invalidateStateRequests() {
        stateGeneration = UUID()
        polling = false
        csrfToken = nil
    }

    @objc func toggleSampleBoard() { setBoardMode(sample: !sampleModeSelected) }

    func setBoardMode(sample: Bool) {
        guard runtime.storeDistribution, !fixtureMode, !startingCollector,
              sample != runtime.sampleMode || startupModeError != nil else { return }
        startingCollector = true
        invalidateStateRequests()
        notificationIntent = UUID()
        requestingPermission = false
        receivedInitialSnapshot = false
        collectorAvailable = false
        chats = []
        statusItem?.button?.title = " …"
        webView.stopLoading()
        window.contentView = webView
        webView.loadHTMLString("<!doctype html><html><body style='font:16px -apple-system;padding:48px;color:#777'><p>Agent Board is changing boards.</p></body></html>", baseURL: nil)
        updateMenu()
        let generation = stateGeneration
        runtime.stopAndWait { [weak self] in
            guard let self, self.stateGeneration == generation else { return }
            for url in self.accessURLs { url.stopAccessingSecurityScopedResource() }
            self.accessURLs.removeAll()
            self.sourceURLs.removeAll()
            do { try self.runtime.setSampleMode(sample) }
            catch {
                if sample { self.startupModeError = error; self.defaults.set(true, forKey: "sampleBoardSelected") }
                self.startingCollector = false
                self.updateMenu()
                self.presentStartupError(error)
                return
            }
            self.startupModeError = nil
            self.defaults.set(sample, forKey: "sampleBoardSelected")
            self.loadAttentionLedger()
            let port = self.defaults.integer(forKey: self.collectorPortKey)
            self.runtime.preferredPort = (1024...65535).contains(port) ? port : 8769
            self.startingCollector = false
            if !sample && !self.defaults.bool(forKey: "setupCompletedV1") {
                self.window.title = "Agent Board"
                self.showSetup()
            } else { self.startCollector() }
        }
    }

    func installAccessScript() {
        let saved: Any = defaults.dictionary(forKey: displayPreferencesKey) ?? [:]
        guard !fixtureMode,
              let originData = try? JSONSerialization.data(withJSONObject: [boardURL.absoluteString.dropLast().description, runtime.accessToken, saved, stateGeneration.uuidString]),
              let arguments = String(data: originData, encoding: .utf8) else { return }
        webView.configuration.userContentController.removeAllUserScripts()
        let script = """
        (() => {
          const [origin, token, saved, generation] = \(arguments);
          if (location.origin !== origin || (location.pathname !== '/' && location.pathname !== '/index.html')) return;
          Object.defineProperty(window, '__agentBoardAccessToken', { value: token, configurable: true });
          const keys = ['theme-id', 'mode', 'motion', 'layout', 'sound', 'sound-id', 'selected', 'palette', 'theme'].map(key => 'agent-board:' + key);
          const set = Storage.prototype.setItem;
          const remove = Storage.prototype.removeItem;
          const clear = Storage.prototype.clear;
          const publish = () => {
            const values = {};
            for (const key of keys) { const value = localStorage.getItem(key); if (value !== null && value.length <= 512) values[key] = value; }
            window.webkit.messageHandlers.agentBoard.postMessage({ kind: 'displayPreferences', generation, values });
          };
          try {
            if (saved !== null) for (const key of keys) {
              if (typeof saved[key] === 'string') set.call(localStorage, key, saved[key]);
              else remove.call(localStorage, key);
            }
            Storage.prototype.setItem = function(key, value) { set.call(this, key, value); if (this === localStorage && keys.includes(String(key))) publish(); };
            Storage.prototype.removeItem = function(key) { remove.call(this, key); if (this === localStorage && keys.includes(String(key))) publish(); };
            Storage.prototype.clear = function() { clear.call(this); if (this === localStorage) publish(); };
            document.addEventListener('DOMContentLoaded', publish, { once: true });
          } catch (_) {}
        })();
        """
        webView.configuration.userContentController.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
    }

    @objc func authorizeClaude() {
        guard runtime.hasBundle, !runtime.sandboxed, !fixtureMode, !authorizingClaude else { return }
        let explanation = NSAlert()
        explanation.messageText = "Allow access to your Claude sign-in?"
        explanation.informativeText = "macOS may ask to let Agent Board read Claude’s saved sign-in from Keychain. This supports regular Claude chats, account usage and project status. This action only checks credential access; it does not send a request to Anthropic or modify your chats. If you decline, local Codex and Claude Code tracking still works."
        explanation.addButton(withTitle: "Continue")
        explanation.addButton(withTitle: "Cancel")
        guard explanation.runModal() == .alertFirstButtonReturn else { return }
        authorizingClaude = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let child = Process()
            child.executableURL = self.runtime.executable
            child.arguments = ["--authorize-claude"]
            let output = Pipe()
            child.standardOutput = output
            child.standardError = FileHandle.nullDevice
            self.credentialHelper = child
            var success = false
            do {
                try child.run()
                let bytes = output.fileHandleForReading.readDataToEndOfFile()
                child.waitUntilExit()
                let value = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any]
                success = child.terminationStatus == 0 && value?["ok"] as? Bool == true
            } catch { }
            DispatchQueue.main.async {
                self.credentialHelper = nil
                self.authorizingClaude = false
                let result = NSAlert()
                result.messageText = success ? "Claude account access is available." : "Claude account access was not granted."
                result.informativeText = success
                    ? "You can enable regular Claude chats, account usage and Claude project status in Setup and Permissions."
                    : "Regular Claude chats, account usage and project status require access to the account signed in to Claude. Confirm that Claude is installed and signed in, then try Allow Claude Account Access again. Local Codex and Claude Code tracking can continue."
                result.addButton(withTitle: "OK")
                result.runModal()
            }
        }
    }

    func showCollectorFailurePage(_ error: Error) {
        let content = NSView(frame: window.contentView?.frame ?? .zero)
        let heading = NSTextField(labelWithString: "Monitoring is unavailable.")
        heading.font = .systemFont(ofSize: 22, weight: .semibold)
        let detail = NSTextField(wrappingLabelWithString: error.localizedDescription)
        detail.font = .systemFont(ofSize: 14)
        detail.textColor = .secondaryLabelColor
        let retry = NSButton(title: "Retry Monitoring", target: self, action: #selector(retryMonitoring))
        retry.bezelStyle = .rounded
        retry.keyEquivalent = "\r"
        retry.isEnabled = !startingCollector
        let stack = NSStackView(views: [heading, detail, retry])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 36),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -36),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 44),
            detail.widthAnchor.constraint(lessThanOrEqualToConstant: 600)
        ])
        window.contentView = content
        collectorRetryButton = retry
        window.makeFirstResponder(retry)
    }

    func showSetupPendingPage() {
        let content = NSView(frame: window.contentView?.frame ?? .zero)
        let heading = NSTextField(labelWithString: "Agent Board is not set up yet.")
        heading.font = .systemFont(ofSize: 22, weight: .semibold)
        let detail = NSTextField(wrappingLabelWithString: runtime.storeDistribution
            ? "Open Setup to select your folders, or try the sample board."
            : "Open Setup to choose your monitoring preferences.")
        detail.font = .systemFont(ofSize: 14)
        detail.textColor = .secondaryLabelColor
        let setup = NSButton(title: "Set Up Monitoring…", target: self, action: #selector(showSetup))
        setup.bezelStyle = .rounded
        setup.keyEquivalent = "\r"
        var views: [NSView] = [heading, detail, setup]
        if runtime.storeDistribution {
            let sample = NSButton(title: "Try Sample Board", target: self, action: #selector(toggleSampleBoard))
            sample.bezelStyle = .rounded
            views.append(sample)
        }
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 36),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -36),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 44),
            detail.widthAnchor.constraint(lessThanOrEqualToConstant: 600)
        ])
        webView.stopLoading()
        window.contentView = content
        collectorRetryButton = nil
        window.makeFirstResponder(setup)
        updateMenu()
    }

    @objc func retryMonitoring() {
        guard !startingCollector else { return }
        if startupModeError != nil { startCollector(); return }
        if !runtime.sampleMode && (runtime.needsLegacyMigration || !defaults.bool(forKey: "setupCompletedV1")) { showSetup() }
        else { startCollector() }
    }

    func openCollectorLog() {
        guard startupModeError == nil else { return }
        NSWorkspace.shared.open(runtime.logURL)
    }

    func handleStartupErrorChoice(_ response: NSApplication.ModalResponse) {
        switch response {
        case .alertFirstButtonReturn: retryMonitoring()
        case .alertSecondButtonReturn: openCollectorLog()
        default: NSApp.terminate(nil)
        }
    }

    func presentStartupError(_ error: Error) {
        showCollectorFailurePage(error)
        let alert = NSAlert()
        alert.messageText = "Agent Board could not start."
        alert.informativeText = error.localizedDescription
        alert.addButton(withTitle: "Retry")
        alert.addButton(withTitle: "Open Local Log").isEnabled = startupModeError == nil
        alert.addButton(withTitle: "Quit")
        handleStartupErrorChoice(alert.runModal())
    }

    var usExternalSupportEnabled: Bool {
        !fixtureMode && runtime.storeDistribution
            && (Bundle.main.object(forInfoDictionaryKey: "AgentBoardStoreSupportStrategy") as? String) == "us-external"
    }

    func isLocalAbout(_ value: URL?) -> Bool {
        guard let value else { return false }
        return value.scheme == boardURL.scheme && value.host == boardURL.host && value.port == boardURL.port
            && value.user == nil && value.password == nil && value.query == nil
            && (value.path == "/about" || value.path == "/about/" || value.path == "/about/index.html")
    }

    func canPresentExternalSupport() -> Bool {
        usExternalSupportEnabled && webView != nil && isLocalAbout(webView.url) && boardIsVisibleAndActive
    }

    func renderExternalSupport(_ visible: Bool) {
        guard webView != nil else { return }
        webView.evaluateJavaScript(externalSupportSectionScript(visible: visible && canPresentExternalSupport()))
    }

    func refreshExternalSupport() {
        guard canPresentExternalSupport() else { stopExternalSupport(); return }
        if externalSupport == nil {
            externalSupport = ExternalSupportController(strategy: "us-external", storeBuild: true,
                country: { let storefront = await StoreKit.Storefront.current; return storefront?.countryCode },
                canPresent: { [weak self] in self?.canPresentExternalSupport() == true },
                render: { [weak self] visible in self?.renderExternalSupport(visible) },
                open: { NSWorkspace.shared.open(fixedKoFiURL("https://ko-fi.com/spacelydigital")!) })
        }
        if supportPath == nil {
            let monitor = NWPathMonitor()
            supportPath = monitor
            monitor.pathUpdateHandler = { [weak self, weak monitor] path in
                DispatchQueue.main.async {
                    guard let self, let monitor, self.supportPath === monitor else { return }
                    self.externalSupport?.connected = path.status == .satisfied
                    self.externalSupport?.refresh()
                }
            }
            monitor.start(queue: DispatchQueue(label: "AgentBoard.StorefrontAvailability"))
        }
        if storefrontUpdates == nil {
            storefrontUpdates = Task { @MainActor [weak self] in
                for await _ in StoreKit.Storefront.updates {
                    guard !Task.isCancelled else { return }
                    self?.externalSupport?.refresh()
                }
            }
        }
        externalSupport?.refresh()
    }

    func stopExternalSupport() {
        externalSupport?.invalidate()
        externalSupport?.connected = false
        supportPath?.cancel()
        supportPath = nil
        storefrontUpdates?.cancel()
        storefrontUpdates = nil
    }

    func handleKoFiNavigation(_ action: WKNavigationAction) -> Bool {
        guard runtime.storeDistribution, let url = action.request.url,
              fixedKoFiURL(url.absoluteString) != nil else { return false }
        if action.navigationType == .linkActivated, action.sourceFrame.isMainFrame,
           isLocalAbout(action.sourceFrame.request.url), canPresentExternalSupport() {
            externalSupport?.refresh(openAfterCheck: true)
        }
        return true
    }

    @objc func showAbout() {
        showBoard()
        webView.load(URLRequest(url: boardURL.appendingPathComponent("about/")))
    }

    @objc func openLocalData() {
        guard startupModeError == nil else { return }
        let directory = runtime.support.appendingPathComponent("data", isDirectory: true)
        guard FileManager.default.fileExists(atPath: directory.path) else {
            showActionError("The local data folder is created when monitoring starts.")
            return
        }
        NSWorkspace.shared.open(directory)
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard !startingCollector, message.frameInfo.isMainFrame, message.frameInfo.securityOrigin.host == boardURL.host,
              message.frameInfo.securityOrigin.port == boardURL.port,
              message.frameInfo.securityOrigin.protocol == "http" else { return }
        if runtime.sandboxed, let body = message.body as? [String: Any], body.count == 3,
           body["kind"] as? String == "openChat", let provider = body["provider"] as? String,
           let value = body["url"] as? String,
           let path = message.frameInfo.request.url?.path, path == "/" || path == "/index.html" {
            openExistingChat(value, provider: provider)
            return
        }
        if let body = message.body as? [String: Any], body["kind"] as? String == "displayPreferences",
           body["generation"] as? String == stateGeneration.uuidString,
           let values = body["values"] as? [String: String], values.count <= 9,
           let path = message.frameInfo.request.url?.path, path == "/" || path == "/index.html" {
            let allowed: [String: Set<String>] = [
                "theme-id": Set(["terminal", "graphite", "paper", "amber", "arctic", "orchid", "moss", "rose", "matrix", "hearth", "aurora", "starfield", "aquarium", "lava", "rain"]),
                "mode": Set(["light", "dark"]), "motion": Set(["true", "false"]),
                "layout": Set(["ledger", "split", "columns", "focus", "instrument"]),
                "sound": Set(["on", "off"]), "theme": Set(["light", "dark"]),
                "sound-id": Set(AgentBoardAlertSound.ids),
                "palette": Set(["terminal", "amber", "arctic", "orchid", "paper"])
            ]
            var valid: [String: String] = [:]
            for (key, value) in values where key.hasPrefix("agent-board:") && value.count <= 512 {
                let suffix = String(key.dropFirst("agent-board:".count))
                if allowed[suffix]?.contains(value) == true || (suffix == "selected" && !value.contains(where: { $0.isNewline })) { valid[key] = value }
            }
            defaults.set(valid, forKey: displayPreferencesKey)
            installAccessScript()
            return
        }
        if message.body as? String == "fullscreen" { toggleFullscreen() }
        else if message.body as? String == "settings", let path = message.frameInfo.request.url?.path,
                path == "/" || path == "/index.html" {
            DispatchQueue.main.async { [weak self] in
                guard NSApp.modalWindow == nil else { return }
                self?.showSetup()
            }
        }
        else if message.body as? String == "sampleBoard", runtime.storeDistribution, !fixtureMode, !sampleModeSelected,
                let url = message.frameInfo.request.url,
                url.scheme == "http", url.host == boardURL.host, url.port == boardURL.port,
                url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
                url.path == "/" || url.path == "/index.html" {
            let generation = stateGeneration
            DispatchQueue.main.async { [weak self] in
                guard let self, self.stateGeneration == generation, NSApp.modalWindow == nil,
                      !self.startingCollector, !self.sampleModeSelected else { return }
                self.setBoardMode(sample: true)
            }
        }
        else if message.body as? String == "theme:dark" { window.appearance = NSAppearance(named: .darkAqua) }
        else if message.body as? String == "theme:light" { window.appearance = NSAppearance(named: .aqua) }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.absoluteString == "about:blank" { decisionHandler(.allow); return }
        if url.scheme == boardURL.scheme && url.host == boardURL.host && url.port == boardURL.port {
            decisionHandler(.allow)
        } else {
            if handleKoFiNavigation(navigationAction) {
                decisionHandler(.cancel)
                return
            }
            if navigationAction.navigationType == .linkActivated, isSupportURL(url) || isUsagePageLink(navigationAction) {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }

    func isSupportURL(_ url: URL) -> Bool {
        if url.absoluteString.lowercased() == "mailto:spacelydigital@gmail.com" { return true }
        if runtime.storeDistribution { return false }
        return url.scheme == "https" && url.host == "ko-fi.com"
            && (url.path == "/spacelydigital" || url.path == "/spacelydigital/")
            && url.user == nil && url.password == nil && url.port == nil
            && url.query == nil && url.fragment == nil
    }

    func isUsagePageLink(_ action: WKNavigationAction) -> Bool {
        guard !runtime.storeDistribution, !runtime.sampleMode, action.sourceFrame.isMainFrame,
              let source = action.sourceFrame.request.url, source.scheme == boardURL.scheme,
              source.host == boardURL.host, source.port == boardURL.port,
              source.path == "/" || source.path == "/index.html",
              let destination = action.request.url?.absoluteString else { return false }
        return destination == "https://claude.ai/settings/usage" || destination == "https://chatgpt.com/codex/settings/usage"
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if handleKoFiNavigation(navigationAction) { return nil }
        if navigationAction.navigationType == .linkActivated,
           let url = navigationAction.request.url, isSupportURL(url) || isUsagePageLink(navigationAction) { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard !startingCollector else { return }
        retryTimer?.invalidate()
        retryTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { [weak self] _ in self?.reload() }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        stopExternalSupport()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        publishFullscreenState()
        refreshExternalSupport()
    }

    func applicationDidBecomeActive(_ notification: Notification) { refreshPermission(); poll(); refreshExternalSupport() }
    func applicationDidResignActive(_ notification: Notification) { stopExternalSupport() }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showBoard(); return true }
    func applicationWillTerminate(_ notification: Notification) {
        stopExternalSupport()
        notificationIntent = UUID()
        invalidateStateRequests()
        pollTimer?.invalidate()
        runtime.stop()
        if credentialHelper?.isRunning == true { credentialHelper?.terminate() }
        for url in accessURLs { url.stopAccessingSecurityScopedResource() }
        session.invalidateAndCancel()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
