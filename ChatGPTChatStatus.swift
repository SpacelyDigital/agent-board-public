import CryptoKit
import Foundation

/// Reads only an identified regular ChatGPT conversation and the controls
/// around its composer. In-memory desktop routes can expose a window title
/// and composer state even when the active conversation URL is unavailable.
enum ChatGPTChatStatus {
    static var isTrusted: Bool { ClaudeChatStatus.isTrusted }

    static func readWithDiagnostics() -> ClaudeChatStatus.Scan {
        #if AGENT_BOARD_DIRECT
        guard isTrusted else { return .init(readings: [], diagnostics: .init()) }
        return DesktopChatAccessibilityReader(profile: profile).scan()
        #else
        return .init(readings: [], diagnostics: .init())
        #endif
    }

    static func chatID(from url: URL) -> String? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme?.lowercased() == "https", parts.percentEncodedHost?.lowercased() == "chatgpt.com",
              parts.user == nil, parts.password == nil, parts.port == nil || parts.port == 443 else { return nil }
        return chatID(fromPath: parts.percentEncodedPath)
    }

    static func isDesktopShell(_ url: URL) -> Bool {
        guard let parts = desktopParts(url) else { return false }
        return parts.percentEncodedPath == "/index.html" || parts.percentEncodedPath == "/detached-window.html"
    }

    static func currentDesktopLinkID(from url: URL, current: String?) -> String? {
        guard current == "page" else { return nil }
        if let id = chatID(from: url) { return id }
        guard let parts = desktopParts(url) else { return nil }
        return chatID(fromPath: parts.percentEncodedPath)
    }

    private static func chatID(fromPath path: String) -> String? {
        if let id = ClaudeChatStatus.chatID(fromPath: path, prefix: "/c/") { return id }
        let segments = path.split(separator: "/", omittingEmptySubsequences: false)
        guard segments.count == 5, segments[0].isEmpty, segments[1] == "g", segments[3] == "c",
              segments[2].hasPrefix("g-p-") else { return nil }
        let project = segments[2].utf8.dropFirst(4)
        guard (1...96).contains(project.count), let first = project.first, isASCIIAlphanumeric(first),
              project.allSatisfy({ isASCIIAlphanumeric($0) || $0 == 45 || $0 == 95 }) else { return nil }
        return ClaudeChatStatus.chatID(fromPath: "/c/" + segments[4], prefix: "/c/")
    }

    private static func isASCIIAlphanumeric(_ byte: UInt8) -> Bool {
        (48...57).contains(byte) || (65...90).contains(byte) || (97...122).contains(byte)
    }

    private static func desktopParts(_ url: URL) -> URLComponents? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme == "app", parts.percentEncodedHost == "-", parts.user == nil,
              parts.password == nil, parts.port == nil else { return nil }
        return parts
    }

    static func displayTitle(from pageTitle: String?) -> String {
        var title = String((pageTitle ?? "").prefix(200)).split(whereSeparator: \.isWhitespace).joined(separator: " ")
        for suffix in [" - ChatGPT", " – ChatGPT"] where title.hasSuffix(suffix) {
            title.removeLast(suffix.count)
            break
        }
        for prefix in ["ChatGPT - ", "ChatGPT – "] where title.hasPrefix(prefix) {
            title.removeFirst(prefix.count)
            break
        }
        return title.isEmpty || title == "ChatGPT" ? "ChatGPT chat" : String(title.prefix(160))
    }

    static func windowChatTitle(windowTitle: String?, pageTitle: String?) -> String? {
        let window = nonGenericTitle(windowTitle)
        let page = nonGenericTitle(pageTitle)
        if let window, let page, window != page { return nil }
        return window ?? page
    }

    private static func nonGenericTitle(_ raw: String?) -> String? {
        guard let raw, raw.utf8.count <= 2_048 else { return nil }
        var title = raw.precomposedStringWithCanonicalMapping.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        for suffix in [" - ChatGPT", " – ChatGPT", " — ChatGPT"] where title.hasSuffix(suffix) {
            title.removeLast(suffix.count)
            break
        }
        for prefix in ["ChatGPT - ", "ChatGPT – ", "ChatGPT — "] where title.hasPrefix(prefix) {
            title.removeFirst(prefix.count)
            break
        }
        let generic: Set<String> = ["chatgpt", "chatgpt chat", "codex", "openai", "chat", "chats", "home",
                                    "new chat", "new conversation", "untitled", "untitled chat", "untitled conversation",
                                    "settings", "activity", "search", "temporary chat"]
        guard !title.isEmpty, !generic.contains(title.lowercased()),
              !title.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains) else { return nil }
        return title
    }

    static func windowChatIdentifier(windowToken: String, title: String) -> String {
        let digest = SHA256.hash(data: Data((windowToken + "\u{0}" + title).utf8))
        return "window:" + digest.prefix(16).map { String(format: "%02x", $0) }.joined()
    }

    #if AGENT_BOARD_DIRECT
    static let profile = DesktopChatAccessibilityReader.Profile(
        bundleID: "com.openai.codex",
        chatID: chatID(from:),
        displayTitle: displayTitle(from:),
        fallbackTitle: "ChatGPT chat",
        composerLabels: ["Ask ChatGPT", "Work with ChatGPT", "Ask anything", "Ask anything…", "Message ChatGPT"],
        stopLabels: ["Stop", "Stop generating", "Stop response", "Stop streaming"],
        idleLabels: ["Send", "Send prompt", "Send message", "Start voice mode", "Use voice mode",
                     "Start voice chat", "Start new voice chat"],
        responseLabels: ["ChatGPT is responding", "ChatGPT is generating"],
        windowLimit: 8,
        searchNestedWebAreas: true,
        shellIdentity: .init(acceptsURL: isDesktopShell, currentLinkID: currentDesktopLinkID(from:current:),
                             navigationLabels: ["Chat history", "Home"],
                             titleFallback: .init(chatTitle: windowChatTitle(windowTitle:pageTitle:),
                                                  identifier: windowChatIdentifier(windowToken:title:),
                                                  composerLabel: "Ask ChatGPT", stopLabel: "Stop", idleLabel: "Send",
                                                  providerControlLabels: ["Select ChatGPT model"]))
    )
    #endif
}
