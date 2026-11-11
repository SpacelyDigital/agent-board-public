import Foundation

#if AGENT_BOARD_DIRECT
import AppKit
import ApplicationServices
#endif

/// A view of regular chats currently open in Claude Desktop. This reader does
/// not infer a completion when a window closes or its controls cannot be read.
enum ClaudeChatStatus {
    enum Status: String, Sendable {
        case cooking, idle
    }

    struct Reading: Equatable, Sendable {
        let id: String
        let title: String
        let status: Status
    }

    struct Diagnostics: CustomStringConvertible, Sendable {
        var applications = 0
        var windows = 0
        var visibleWindows = 0
        var webAreas = 0
        var pageURLs = 0
        var shellURLs = 0
        var acceptedShells = 0
        var navigationLandmarks = 0
        var historyLandmarks = 0
        var historyLinks = 0
        var searchedHistoryLinks = 0
        var currentLinks = 0
        var currentLinkURLs = 0
        var verifiedPages = 0
        var mainPanes = 0
        var textInputs = 0
        var composers = 0
        var buttons = 0
        var sendControls = 0
        var completeIdleScopes = 0
        var incompleteControlScopes = 0
        var childCountUnavailable = 0
        var incompleteChildBatches = 0
        var emptyChildValues = 0
        var childTruncations = 0
        var childCountMismatches = 0
        var childCopyFailures = 0
        var depthTruncations = 0
        var maximumChildCount = 0
        var controlReadFailures = 0
        var controlChildFailures = 0
        var controlDepthTruncations = 0
        var maximumControlChildCount = 0
        var stopControls = 0
        var respondingMarkers = 0
        var classifications = 0
        var nodes = 0
        var roleReadFailures = 0
        var arrayReadFailures = 0
        var axTimeouts = 0
        var walkLimits = 0
        var deadlineReached = false
        var requestLimitReached = false

        var counters: [String: Int] {
            [
                "apps": applications, "windows": windows, "visible_windows": visibleWindows,
                "web_areas": webAreas, "page_urls": pageURLs, "shell_urls": shellURLs,
                "accepted_shells": acceptedShells, "navigation_landmarks": navigationLandmarks,
                "history_landmarks": historyLandmarks, "history_links": historyLinks,
                "searched_history_links": searchedHistoryLinks,
                "current_links": currentLinks, "current_link_urls": currentLinkURLs,
                "walk_limits": walkLimits, "role_failures": roleReadFailures,
                "recognized_pages": verifiedPages, "text_inputs": textInputs, "composers": composers,
                "stop_controls": stopControls, "idle_controls": sendControls, "classifications": classifications,
                "complete_idle_scopes": completeIdleScopes, "incomplete_control_scopes": incompleteControlScopes,
                "child_count_unavailable": childCountUnavailable, "incomplete_child_batches": incompleteChildBatches,
                "empty_child_values": emptyChildValues,
                "child_truncations": childTruncations, "child_count_mismatches": childCountMismatches,
                "child_copy_failures": childCopyFailures, "depth_truncations": depthTruncations,
                "max_child_count": maximumChildCount, "control_read_failures": controlReadFailures,
                "control_child_failures": controlChildFailures, "control_depth_truncations": controlDepthTruncations,
                "max_control_child_count": maximumControlChildCount,
                "deadline_reached": deadlineReached ? 1 : 0, "request_limit_reached": requestLimitReached ? 1 : 0,
            ]
        }

        // Only fixed field names and numeric/boolean counters. Never include IDs,
        // URLs, labels, titles, or arbitrary AX errors in a diagnostic message.
        var description: String {
            "apps=\(applications) windows=\(windows) visible=\(visibleWindows) webAreas=\(webAreas) urls=\(pageURLs) " +
            "verified=\(verifiedPages) main=\(mainPanes) inputs=\(textInputs) composers=\(composers) buttons=\(buttons) " +
            "send=\(sendControls) stop=\(stopControls) responding=\(respondingMarkers) classified=\(classifications) " +
            "nodes=\(nodes) roleFailures=\(roleReadFailures) arrayFailures=\(arrayReadFailures) timeouts=\(axTimeouts) " +
            "walkLimits=\(walkLimits) deadline=\(deadlineReached) requestLimit=\(requestLimitReached)"
        }
    }

    struct Scan: Sendable {
        let readings: [Reading]
        let diagnostics: Diagnostics
    }

    static var isTrusted: Bool {
        #if AGENT_BOARD_DIRECT
        AXIsProcessTrusted()
        #else
        false
        #endif
    }

    /// Only the explicit settings action should call this. Polling never prompts.
    @discardableResult static func requestPermission() -> Bool {
        #if AGENT_BOARD_DIRECT
        AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        #else
        false
        #endif
    }

    /// Synchronous and bounded. Call this on a background queue, not the UI thread.
    static func read() -> [Reading] {
        readWithDiagnostics().readings
    }

    static func readWithDiagnostics() -> Scan {
        #if AGENT_BOARD_DIRECT
        guard isTrusted else { return Scan(readings: [], diagnostics: Diagnostics()) }
        return DesktopChatAccessibilityReader(profile: .claude).scan()
        #else
        return Scan(readings: [], diagnostics: Diagnostics())
        #endif
    }

    static func chatID(from url: URL) -> String? {
        chatID(from: url, host: "claude.ai", pathPrefix: "/chat/")
    }

    static func chatID(from url: URL, host: String, pathPrefix: String) -> String? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme?.lowercased() == "https", parts.percentEncodedHost?.lowercased() == host,
              parts.user == nil, parts.password == nil, parts.port == nil || parts.port == 443 else { return nil }
        return chatID(fromPath: parts.percentEncodedPath, prefix: pathPrefix)
    }

    static func chatID(fromPath path: String, prefix: String) -> String? {
        guard path.hasPrefix(prefix) else { return nil }
        let rawID = String(path.dropFirst(prefix.count))
        guard rawID.utf8.count == 36, let uuid = UUID(uuidString: rawID),
              uuid.uuidString.lowercased() == rawID.lowercased() else { return nil }
        return uuid.uuidString.lowercased()
    }

    static func displayTitle(from pageTitle: String?) -> String {
        var title = String((pageTitle ?? "").prefix(200)).split(whereSeparator: \.isWhitespace).joined(separator: " ")
        for suffix in [" - Claude", " – Claude"] where title.hasSuffix(suffix) {
            title.removeLast(suffix.count)
            break
        }
        return title.isEmpty || title == "Claude" ? "Claude chat" : String(title.prefix(160))
    }

    static func classify(composer: Bool, stop: Bool, responding: Bool, send: Bool,
                         idleScopeComplete: Bool) -> Status? {
        guard composer else { return nil }
        if stop || responding { return .cooking }
        return send && idleScopeComplete ? .idle : nil
    }
}

#if AGENT_BOARD_DIRECT
final class DesktopChatAccessibilityReader {
    struct WindowTitleFallback {
        let chatTitle: (String?, String?) -> String?
        let identifier: (String, String) -> String
        let composerLabel: String
        let stopLabel: String
        let idleLabel: String
        // A shared Electron shell and composer are not provider identity.
        // Require a provider-specific control in the same composer container.
        let providerControlLabels: Set<String>

        func acceptsProviderControl(role: String, label: String, enabled: Bool, hidden: Bool) -> Bool {
            role == "AXButton" && enabled && !hidden && providerControlLabels.contains(label)
        }
    }

    struct ShellIdentity {
        let acceptsURL: (URL) -> Bool
        let currentLinkID: (URL, String?) -> String?
        let navigationLabels: Set<String>
        let titleFallback: WindowTitleFallback?
    }

    struct Profile {
        let bundleID: String
        let chatID: (URL) -> String?
        let displayTitle: (String?) -> String
        let fallbackTitle: String
        let composerLabels: Set<String>
        let stopLabels: Set<String>
        let idleLabels: Set<String>
        let responseLabels: Set<String>
        let windowLimit: Int
        let searchNestedWebAreas: Bool
        let shellIdentity: ShellIdentity?

        static var claude: Self {
            .init(bundleID: "com.anthropic.claudefordesktop",
                  chatID: ClaudeChatStatus.chatID(from:), displayTitle: ClaudeChatStatus.displayTitle(from:),
                  fallbackTitle: "Claude chat", composerLabels: ["Write your prompt to Claude"],
                  stopLabels: ["Stop response"], idleLabels: ["Send message", "Use voice mode"],
                  responseLabels: ["Claude is responding"], windowLimit: 4, searchNestedWebAreas: false,
                  shellIdentity: nil)
        }
    }

    private static let containers: Set<String> = [
        "AXWindow", "AXWebArea", "AXGroup", "AXSplitGroup", "AXScrollArea", "AXLayoutArea",
        "AXLayoutItem", "AXToolbar", "AXPane", "AXSection", "AXForm", "AXUnknown",
        "AXWebApplication", "AXLandmarkMain", "AXLandmarkRegion",
    ]
    private static let ignoredLandmarks: Set<String> = [
        "AXLandmarkNavigation", "AXLandmarkComplementary", "AXLandmarkSearch",
    ]
    private static let navigationContainers: Set<String> = [
        "AXLandmarkNavigation", "AXLandmarkComplementary", "AXList", "AXListItem", "AXOutline", "AXRow",
        "AXMenu", "AXMenuItem", "AXTabGroup", "AXDisclosureTriangle",
    ]

    // This allowlist intentionally has no AXValue, text ranges, or selected text.
    private enum Attribute: String {
        case windows = "AXWindows", children = "AXChildren"
        case mainWindow = "AXMainWindow", focusedWindow = "AXFocusedWindow", visibleChildren = "AXVisibleChildren"
        case role = "AXRole", subrole = "AXSubrole", hidden = "AXHidden", minimized = "AXMinimized"
        case url = "AXURL", title = "AXTitle", description = "AXDescription", enabled = "AXEnabled"
        case placeholder = "AXPlaceholderValue", help = "AXHelp", ariaCurrent = "AXARIACurrent"
        case label = "AXLabelValue", titleElement = "AXTitleUIElement"
    }

    private struct Node {
        let element: AXUIElement
        let parents: [AXUIElement]
    }

    private struct CurrentLink {
        let element: AXUIElement
        let id: String
    }

    // An AX window is an opaque object. Compare references by CFEqual rather
    // than treating a title, position, or pointer hash as a window identifier.
    private final class WindowTokens: @unchecked Sendable {
        private struct Entry {
            let application: String
            let window: AXUIElement
            let token: String
        }

        private let lock = NSLock()
        private var entries: [Entry] = []

        func token(for window: AXUIElement, application: String) -> String {
            lock.lock()
            defer { lock.unlock() }
            if let index = entries.firstIndex(where: { $0.application == application && CFEqual($0.window, window) }) {
                let entry = entries.remove(at: index)
                entries.append(entry)
                return entry.token
            }
            if entries.count >= 32 { entries.removeFirst() }
            let token = UUID().uuidString.lowercased()
            entries.append(.init(application: application, window: window, token: token))
            return token
        }
    }

    private static let windowTokens = WindowTokens()

    private let deadline = ProcessInfo.processInfo.systemUptime + 2.2
    private var requests: Int
    private var readFailures = 0
    private var diagnostics = ClaudeChatStatus.Diagnostics()
    private var scanningControls = false
    private let profile: Profile
    private var available: Bool { requests > 0 && ProcessInfo.processInfo.systemUptime < deadline }

    init(profile: Profile) {
        self.profile = profile
        self.requests = profile.shellIdentity == nil ? 900 : 1_600
    }

    func scan() -> ClaudeChatStatus.Scan {
        var readings: [String: ClaudeChatStatus.Reading] = [:]
        for app in NSRunningApplication.runningApplications(withBundleIdentifier: profile.bundleID).prefix(2) {
            guard available else { break }
            diagnostics.applications += 1
            guard app.bundleIdentifier == profile.bundleID, !app.isTerminated else { continue }
            let root = AXUIElementCreateApplication(app.processIdentifier)
            var windows: [AXUIElement] = []
            for candidate in [element(root, .mainWindow), element(root, .focusedWindow)].compactMap({ $0 })
                + elements(root, .windows, limit: profile.windowLimit) {
                if !windows.contains(where: { CFEqual($0, candidate) }) { windows.append(candidate) }
            }
            diagnostics.windows += windows.count
            for window in windows.prefix(profile.windowLimit) {
                guard available else { break }
                // Minimized windows and hidden apps can still expose their
                // current controls. Only unreadable/hidden content is unknown.
                guard flag(window, .hidden) != true else { continue }
                diagnostics.visibleWindows += 1
                let application = "\(app.processIdentifier):\(app.launchDate?.timeIntervalSinceReferenceDate ?? -1)"
                guard let reading = read(window: window, application: application) else { continue }
                if readings[reading.id]?.status != .cooking { readings[reading.id] = reading }
            }
        }
        diagnostics.classifications = readings.count
        diagnostics.deadlineReached = ProcessInfo.processInfo.systemUptime >= deadline
        diagnostics.requestLimitReached = requests == 0
        return .init(readings: readings.values.sorted { $0.id < $1.id }, diagnostics: diagnostics)
    }

    private func read(window: AXUIElement, application: String) -> ClaudeChatStatus.Reading? {
        var page: AXUIElement?
        var pageID: String?
        var shellURL: URL?
        walk(window, maxNodes: 112, maxDepth: 12, descendIntoWebAreas: profile.searchNestedWebAreas) { node, role, _ in
            guard role == "AXWebArea" else { return false }
            self.diagnostics.webAreas += 1
            guard self.flag(node.element, .hidden) != true,
                  let url = self.pageURL(of: node.element) else { return false }
            self.diagnostics.pageURLs += 1
            if url.scheme?.lowercased() == "app" { self.diagnostics.shellURLs += 1 }
            if let id = self.profile.chatID(url) {
                page = node.element
                pageID = id
                return true
            }
            if self.profile.shellIdentity?.acceptsURL(url) == true {
                self.diagnostics.acceptedShells += 1
                page = node.element
                shellURL = url
                return true
            }
            return false
        }
        guard let page else { return nil }
        let fallback = pageID == nil && shellURL != nil ? profile.shellIdentity?.titleFallback : nil
        let fallbackTitle = fallback.flatMap { windowChatTitle(window: window, page: page, using: $0) }
        let useTitle = fallbackTitle != nil
        let link = shellURL != nil && !useTitle ? profile.shellIdentity.flatMap { currentChatLink(in: page, using: $0) } : nil
        let exactID = pageID ?? link?.id
        guard exactID != nil || fallbackTitle != nil else { return nil }
        diagnostics.verifiedPages += 1

        // The shell fallback must not broaden its provider-specific composer
        // label with generic/Work labels accepted on an exact chat URL.
        let composerLabels = useTitle ? Set(fallback.map { [$0.composerLabel] } ?? []) : profile.composerLabels
        let stopLabels = profile.stopLabels.union(fallback.map { [$0.stopLabel] } ?? [])
        let idleLabels = profile.idleLabels.union(fallback.map { [$0.idleLabel] } ?? [])
        let pageNode = Node(element: page, parents: [])
        var main: Node?
        walk(page, maxNodes: 36, maxDepth: 7, omitScrollAreas: true) { node, role, subrole in
            guard role == "AXLandmarkMain" || subrole == "AXLandmarkMain" else { return false }
            main = node
            return true
        }
        if main != nil { diagnostics.mainPanes += 1 }
        var composer: Node?
        var responding = false
        func lookForComposer(in scope: Node, omitScrollAreas: Bool, maxNodes: Int) {
            walk(scope.element, parents: scope.parents, maxNodes: maxNodes, maxDepth: 23,
                 tailFirst: true, omitScrollAreas: omitScrollAreas) { node, role, subrole in
                if role == "AXTextArea" || role == "AXTextField" {
                    self.diagnostics.textInputs += 1
                    if self.flag(node.element, .hidden) != true, self.flag(node.element, .enabled) != false,
                       self.matchesComposer(node.element, labels: composerLabels, desktopShell: useTitle) {
                        composer = node
                        return true
                    }
                }
                // Only an actual status element may expose a response marker.
                if !useTitle, !responding, node.parents.count <= 6,
                   self.isResponseStatus(node.element, role: role, subrole: subrole) { responding = true }
                return false
            }
        }
        // Prefer the main landmark and the tail of its structural branches. A
        // long transcript/sidebar no longer fills the queue before the composer.
        if useTitle {
            if let main { lookForComposer(in: main, omitScrollAreas: false, maxNodes: 130) }
            if composer == nil { lookForComposer(in: pageNode, omitScrollAreas: false, maxNodes: 200) }
        } else {
            if let main { lookForComposer(in: main, omitScrollAreas: true, maxNodes: 105) }
            if composer == nil { lookForComposer(in: pageNode, omitScrollAreas: true, maxNodes: 130) }
            if composer == nil { lookForComposer(in: main ?? pageNode, omitScrollAreas: false, maxNodes: 185) }
        }
        guard let composer else { return nil }
        diagnostics.composers += 1

        var stop = false
        var send = false
        var idleScopeComplete = false
        var providerVerified = !useTitle
        var providerControl: AXUIElement?
        var seenButtons: [AXUIElement] = []
        let controlLabels = stopLabels.union(idleLabels)
        // Keep control-name reads close to the composer and out of the transcript
        // scroll area, recents, and nested web documents.
        for ancestor in composer.parents.suffix(5).reversed() {
            let scopeIsPage = CFEqual(ancestor, page)
            // Do not accept another composer's model picker or a global mode
            // selector from the page/sidebar as evidence for this composer.
            if useTitle && (scopeIsPage || main.map({ CFEqual(ancestor, $0.element) }) == true) { break }
            scanningControls = true
            let initialControlReadFailures = readFailures
            // Claude's finished composer contains nested structural wrappers
            // beyond six levels. Keep the node/request/time bounds, but allow
            // the narrow control scope to finish before considering it idle.
            let scopeComplete = walk(ancestor, maxNodes: 54, maxDepth: scopeIsPage ? 2 : 24,
                                     tailFirst: true, omitScrollAreas: true) { node, role, subrole in
                if role == "AXButton", !seenButtons.contains(where: { CFEqual($0, node.element) }) {
                    seenButtons.append(node.element)
                    self.diagnostics.buttons += 1
                    guard self.flag(node.element, .hidden) != true else { return false }
                    if useTitle, let fallback,
                       let label = self.match(node.element, fallback.providerControlLabels, extra: [.help, .label]),
                       fallback.acceptsProviderControl(role: role, label: label,
                                                       enabled: self.flag(node.element, .enabled) != false, hidden: false) {
                        providerVerified = true
                        providerControl = node.element
                    }
                    guard let label = self.controlLabel(node.element, expected: controlLabels) else { return false }
                    if stopLabels.contains(label), self.flag(node.element, .enabled) != false {
                        stop = true
                        self.diagnostics.stopControls += 1
                        return providerVerified
                    }
                    if idleLabels.contains(label) {
                        send = true
                        self.diagnostics.sendControls += 1
                    }
                } else if !useTitle && self.isResponseStatus(node.element, role: role, subrole: subrole) {
                    responding = true
                    return true
                }
                return false
            }
            scanningControls = false
            diagnostics.controlReadFailures += readFailures - initialControlReadFailures
            // A complete scan of the nearest composer-control scope is enough
            // to establish idle. Rewalking every wider ancestor after finding
            // Send can exhaust the AX budget on a finished long response.
            // Active controls retain precedence, and truncated scopes cannot
            // establish that no Stop/response control was present.
            if !scopeComplete { diagnostics.incompleteControlScopes += 1 }
            if send && scopeComplete {
                idleScopeComplete = true
                diagnostics.completeIdleScopes += 1
            }
            if providerVerified && (stop || responding || idleScopeComplete) { break }
        }
        if responding { diagnostics.respondingMarkers += 1 }
        guard providerVerified,
              let status = ClaudeChatStatus.classify(composer: true, stop: stop, responding: responding,
                                                    send: send, idleScopeComplete: idleScopeComplete),
              available else { return nil }
        // Recheck the page identity before returning evidence obtained in a UI
        // that can navigate while this scan is underway.
        if let fallback, let fallbackTitle, let shellURL {
            guard pageURL(of: page) == shellURL,
                  windowChatTitle(window: window, page: page, using: fallback) == fallbackTitle,
                  let providerControl,
                  flag(providerControl, .hidden) != true, flag(providerControl, .enabled) != false,
                  match(providerControl, fallback.providerControlLabels, extra: [.help, .label]) != nil else { return nil }
            if requests > 500, let shell = profile.shellIdentity, let verified = currentChatLink(in: page, using: shell),
               readTitle(verified.element) == String(fallbackTitle.prefix(160)),
               pageURL(of: page) == shellURL,
               currentChatID(of: verified.element, using: shell) == verified.id {
                return .init(id: verified.id, title: String(fallbackTitle.prefix(160)), status: status)
            }
            let token = Self.windowTokens.token(for: window, application: application)
            return .init(id: fallback.identifier(token, fallbackTitle), title: String(fallbackTitle.prefix(160)), status: status)
        }
        guard let id = exactID else { return nil }
        let title: String
        if let link, let shellURL, let shell = profile.shellIdentity {
            title = readTitle(link.element)
            guard pageURL(of: page) == shellURL, value(link.element, .role) as? String == "AXLink",
                  currentChatID(of: link.element, using: shell) == id else { return nil }
        } else {
            title = readTitle(page)
            guard pageURL(of: page).flatMap(profile.chatID) == id else { return nil }
        }
        return .init(id: id, title: title, status: status)
    }

    private func windowChatTitle(window: AXUIElement, page: AXUIElement, using fallback: WindowTitleFallback) -> String? {
        fallback.chatTitle(value(window, .title) as? String, value(page, .title) as? String)
    }

    private func currentChatLink(in page: AXUIElement, using shell: ShellIdentity) -> CurrentLink? {
        var navigation: AXUIElement?
        walk(page, maxNodes: 120, maxDepth: 16, includeNavigation: true) { node, role, subrole in
            guard role == "AXLandmarkNavigation" || subrole == "AXLandmarkNavigation" else { return false }
            self.diagnostics.navigationLandmarks += 1
            guard self.flag(node.element, .hidden) != true,
                  self.match(node.element, shell.navigationLabels) != nil else { return false }
            self.diagnostics.historyLandmarks += 1
            navigation = node.element
            return true
        }
        guard let navigation else { return nil }
        var current: CurrentLink?
        var ambiguous = false
        func consider(_ link: AXUIElement) -> Bool {
            diagnostics.historyLinks += 1
            guard let id = currentChatID(of: link, using: shell) else { return false }
            if let current, current.id != id {
                ambiguous = true
                return true
            }
            if current == nil { current = CurrentLink(element: link, id: id) }
            return false
        }
        let links = visibleHistoryLinks(in: navigation)
        if links.isEmpty {
            walk(navigation, maxNodes: 180, maxDepth: 23, includeNavigation: true, leadingDepthFirst: true) { node, role, _ in
                role == "AXLink" && consider(node.element)
            }
        } else {
            diagnostics.searchedHistoryLinks += links.count
            for link in links where value(link, .role) as? String == "AXLink" {
                if consider(link) { break }
            }
        }
        return ambiguous ? nil : current
    }

    private func visibleHistoryLinks(in navigation: AXUIElement) -> [AXUIElement] {
        guard budget(navigation) else { return [] }
        let search: [String: Any] = ["AXSearchKey": "AXLinkSearchKey", "AXVisibleOnly": true,
                                     "AXDirection": "AXDirectionNext", "AXResultsLimit": 64]
        var result: CFTypeRef?
        let status = AXUIElementCopyParameterizedAttributeValue(
            navigation, "AXUIElementsForSearchPredicate" as CFString, search as CFDictionary, &result
        )
        guard status == .success, let links = result as? [AXUIElement], links.count <= 64 else { return [] }
        return links
    }

    private func currentChatID(of link: AXUIElement, using shell: ShellIdentity) -> String? {
        // Read the current-page marker before looking at the URL so non-current
        // links in the user's history never have their destinations read.
        guard let current = value(link, .ariaCurrent) as? String, current == "page",
              flag(link, .hidden) != true else { return nil }
        diagnostics.currentLinks += 1
        guard let url = pageURL(of: link) else { return nil }
        diagnostics.currentLinkURLs += 1
        return shell.currentLinkID(url, current)
    }

    private func readTitle(_ element: AXUIElement) -> String {
        let title = profile.displayTitle(value(element, .title) as? String)
        return title == profile.fallbackTitle ? profile.displayTitle(value(element, .description) as? String) : title
    }

    @discardableResult
    private func walk(_ root: AXUIElement, parents: [AXUIElement] = [], maxNodes: Int, maxDepth: Int,
                      tailFirst: Bool = false, omitScrollAreas: Bool = false,
                      descendIntoWebAreas: Bool = false, includeNavigation: Bool = false, leadingDepthFirst: Bool = false,
                      visit: (Node, String, String?) -> Bool) -> Bool {
        var nodes = [Node(element: root, parents: parents)]
        var index = 0
        var visited = 0
        var unreadableNode = false
        var completeChildren = true
        let initialReadFailures = readFailures
        let useStack = tailFirst || leadingDepthFirst
        while (useStack ? !nodes.isEmpty : index < nodes.count), visited < maxNodes, available {
            let node = useStack ? nodes.removeLast() : nodes[index]
            if !useStack { index += 1 }
            visited += 1
            diagnostics.nodes += 1
            guard let role = value(node.element, .role) as? String else {
                diagnostics.roleReadFailures += 1
                unreadableNode = true
                continue
            }
            let depth = node.parents.count - parents.count
            let subrole = role == "AXGroup" ? value(node.element, .subrole) as? String : nil
            if !includeNavigation, let subrole, Self.ignoredLandmarks.contains(subrole) { continue }
            if visit(node, role, subrole) { return false }
            guard Self.containers.contains(role) || (includeNavigation && Self.navigationContainers.contains(role)),
                  (role != "AXWebArea" || depth == 0 || descendIntoWebAreas),
                  (!omitScrollAreas || role != "AXScrollArea" || depth == 0) else { continue }
            if depth >= maxDepth {
                // A depth boundary is complete only when the container is
                // proven empty. Navigation/transcript exclusions above remain
                // deliberate exclusions from the composer-control scope.
                let boundary = elementBatch(node.element, .children, limit: 1, preferEnd: true)
                if !boundary.complete || !boundary.elements.isEmpty {
                    completeChildren = false
                    diagnostics.depthTruncations += 1
                    if scanningControls { diagnostics.controlDepthTruncations += 1 }
                }
                continue
            }
            var batch: (elements: [AXUIElement], complete: Bool) = ([], false)
            if role == "AXScrollArea", !includeNavigation {
                batch = elementBatch(node.element, .visibleChildren, limit: 24, preferEnd: tailFirst)
            }
            if batch.elements.isEmpty { batch = elementBatch(node.element, .children, limit: 24, preferEnd: tailFirst) }
            if !batch.complete { completeChildren = false }
            let children = batch.elements
            let parents = node.parents + [node.element]
            if leadingDepthFirst {
                nodes.append(contentsOf: children.reversed().map { Node(element: $0, parents: parents) })
            } else {
                nodes.append(contentsOf: children.map { Node(element: $0, parents: parents) })
            }
        }
        if visited == maxNodes && (useStack ? !nodes.isEmpty : index < nodes.count) { diagnostics.walkLimits += 1 }
        return available && !unreadableNode && completeChildren && readFailures == initialReadFailures
            && (useStack ? nodes.isEmpty : index >= nodes.count)
    }

    private func isResponseStatus(_ element: AXUIElement, role: String, subrole: String?) -> Bool {
        guard role == "AXStatus" || role == "AXBusyIndicator" || role == "AXProgressIndicator" ||
              subrole == "AXApplicationStatus" else { return false }
        return flag(element, .hidden) != true && match(element, profile.responseLabels) != nil
    }

    private func matchesComposer(_ input: AXUIElement, labels: Set<String>, desktopShell: Bool) -> Bool {
        let extra: [Attribute] = desktopShell ? [.placeholder, .help, .label] : [.placeholder]
        if match(input, labels, extra: extra) != nil { return true }
        guard desktopShell, let title = element(input, .titleElement) else { return false }
        return match(title, labels, extra: [.label]) != nil
    }

    private func controlLabel(_ control: AXUIElement, expected: Set<String>) -> String? {
        // Electron can expose an icon button's accessible name through its
        // label or title element instead of AXTitle/AXDescription. Read these
        // only for controls inside the verified composer's structural scope.
        if let label = match(control, expected, extra: [.help, .label]) { return label }
        guard let title = element(control, .titleElement) else { return nil }
        return match(title, expected, extra: [.label])
    }

    private func match(_ element: AXUIElement, _ expected: Set<String>, extra: [Attribute] = []) -> String? {
        for attribute in [Attribute.description, .title] + extra {
            if let label = value(element, attribute) as? String,
               label.utf8.count <= 128 {
                let normalized = label.trimmingCharacters(in: .whitespacesAndNewlines)
                if expected.contains(normalized) { return normalized }
            }
        }
        return nil
    }

    private func pageURL(of element: AXUIElement) -> URL? {
        let raw = value(element, .url)
        return (raw as? URL) ?? (raw as? String).flatMap { URL(string: $0) }
    }

    private func element(_ parent: AXUIElement, _ attribute: Attribute) -> AXUIElement? {
        guard let raw = value(parent, attribute), CFGetTypeID(raw) == AXUIElementGetTypeID() else { return nil }
        return unsafeBitCast(raw, to: AXUIElement.self)
    }

    private func flag(_ element: AXUIElement, _ attribute: Attribute) -> Bool? {
        (value(element, attribute) as? NSNumber)?.boolValue
    }

    private func budget(_ element: AXUIElement) -> Bool {
        let remaining = deadline - ProcessInfo.processInfo.systemUptime
        guard requests > 0, remaining > 0.002 else { return false }
        requests -= 1
        let result = AXUIElementSetMessagingTimeout(element, Float(min(0.12, remaining)))
        if result != .success { readFailures += 1 }
        return result == .success
    }

    private func value(_ element: AXUIElement, _ attribute: Attribute) -> CFTypeRef? {
        guard budget(element) else { return nil }
        var raw: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, attribute.rawValue as CFString, &raw)
        if result == .cannotComplete { diagnostics.axTimeouts += 1 }
        if result != .success && result != .attributeUnsupported && result != .noValue { readFailures += 1 }
        return result == .success ? raw : nil
    }

    private func elements(_ element: AXUIElement, _ attribute: Attribute, limit: Int,
                          preferEnd: Bool = false) -> [AXUIElement] {
        elementBatch(element, attribute, limit: limit, preferEnd: preferEnd).elements
    }

    static func completeChildBatch(total: Int?, start: Int, limit: Int, returned: Int) -> Bool {
        guard start == 0, limit > 0, returned >= 0, returned <= limit else { return false }
        if let total { return total >= 0 && total <= limit && returned == total }
        // Some providers do not implement the separate count operation. A
        // successful short copy from index zero still contains every child.
        return returned < limit
    }

    static func absentChildValue(_ result: AXError, knownCount: Int?) -> Bool {
        result == .noValue && (knownCount == nil || knownCount == 0)
    }

    private func elementBatch(_ element: AXUIElement, _ attribute: Attribute, limit: Int,
                              preferEnd: Bool = false) -> (elements: [AXUIElement], complete: Bool) {
        var start = 0
        var total: Int?
        let isChildren = attribute == .children || attribute == .visibleChildren
        if preferEnd {
            guard budget(element) else { return ([], false) }
            var count: CFIndex = 0
            let result = AXUIElementGetAttributeValueCount(element, attribute.rawValue as CFString, &count)
            if result == .cannotComplete { diagnostics.axTimeouts += 1 }
            if result != .success && result != .attributeUnsupported && result != .noValue { readFailures += 1 }
            if result == .success {
                if isChildren {
                    diagnostics.maximumChildCount = max(diagnostics.maximumChildCount, min(10_000, count))
                    if scanningControls {
                        diagnostics.maximumControlChildCount = max(diagnostics.maximumControlChildCount, min(10_000, count))
                    }
                }
                guard count > 0 else { return ([], true) }
                total = count
                start = max(0, count - limit)
            } else if isChildren {
                diagnostics.childCountUnavailable += 1
            }
        }
        guard budget(element) else { return ([], false) }
        var raw: CFArray?
        let result = AXUIElementCopyAttributeValues(element, attribute.rawValue as CFString, start, limit, &raw)
        if result == .cannotComplete { diagnostics.axTimeouts += 1 }
        if result != .success && result != .attributeUnsupported && result != .noValue { readFailures += 1 }
        if isChildren && Self.absentChildValue(result, knownCount: total) {
            diagnostics.emptyChildValues += 1
            return ([], true)
        }
        guard result == .success, let elements = raw as? [AXUIElement] else {
            if result != .attributeUnsupported && result != .noValue { diagnostics.arrayReadFailures += 1 }
            if isChildren {
                diagnostics.incompleteChildBatches += 1
                diagnostics.childCopyFailures += 1
                if scanningControls { diagnostics.controlChildFailures += 1 }
            }
            return ([], false)
        }
        // Without a count, a full batch may be truncated. With a count, require
        // every child and a matching returned length; a failed read is never
        // equivalent to a proven empty container.
        let complete = Self.completeChildBatch(total: total, start: start, limit: limit, returned: elements.count)
        if !complete && isChildren {
            diagnostics.incompleteChildBatches += 1
            if start > 0 || total.map({ $0 > limit }) == true {
                diagnostics.childTruncations += 1
            } else if total != nil {
                diagnostics.childCountMismatches += 1
            }
            if scanningControls { diagnostics.controlChildFailures += 1 }
        }
        return (elements, complete)
    }
}
#endif
