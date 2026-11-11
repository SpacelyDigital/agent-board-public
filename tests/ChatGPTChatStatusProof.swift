import Foundation

/// Run with both status reader files. This proof never queries another app.
@main
struct ChatGPTChatStatusProof {
    static func main() {
        let id = "22222222-2222-4222-8222-222222222222"
        func chatID(_ url: String) -> String? {
            URL(string: url).flatMap(ChatGPTChatStatus.chatID(from:))
        }
        precondition(chatID("https://chatgpt.com/c/\(id.uppercased())?ref=example#bottom") == id)
        let projectPath = "/g/g-p-1029384756abcdef/c/\(id)"
        precondition(chatID("https://chatgpt.com\(projectPath)") == id)
        precondition(ChatGPTChatStatus.currentDesktopLinkID(from: URL(string: "app://-\(projectPath)")!,
                                                           current: "page") == id)
        for url in [
            "https://chatgpt.com/c/\(id)/extra", "https://chatgpt.com/codex/c/\(id)",
            "https://chatgpt.com/share/\(id)", "https://chatgpt.com/codex/thread/\(id)",
            "https://chatgpt.com/c/%32\(id.dropFirst())", "https://chatgpt.com/%63/\(id)",
            "https://user@chatgpt.com/c/\(id)", "https://chatgpt.com@other.example/c/\(id)",
            "https://chatgpt.com.example/c/\(id)", "https://chatgpt.com:8443/c/\(id)",
            "https://other.chatgpt.com/c/\(id)", "http://chatgpt.com/c/\(id)",
            "app://-/index.html?initialRoute=%2Fc%2F\(id)", "chatgpt://c/\(id)",
            "https://chatgpt.com/c/new", "https://claude.ai/chat/\(id)",
            "https://chatgpt.com/g/g-p-/c/\(id)", "https://chatgpt.com/g/g-p-%61/c/\(id)",
            "https://chatgpt.com/g/g-p-a%2Fb/c/\(id)", "https://chatgpt.com/g/g-p-a/shared/c/\(id)",
            "https://chatgpt.com/g/g-custom-gpt/c/\(id)", "https://chatgpt.com\(projectPath)/",
            "https://other.example\(projectPath)", "https://chatgpt.com/g/g-p-\(String(repeating: "a", count: 97))/c/\(id)",
        ] {
            precondition(chatID(url) == nil, "Accepted an unverified chat route: \(url)")
        }
        let shell = URL(string: "app://-/index.html?initialRoute=%2Fc%2F\(id)")!
        precondition(ChatGPTChatStatus.isDesktopShell(shell))
        precondition(ChatGPTChatStatus.isDesktopShell(URL(string: "app://-/detached-window.html")!))
        precondition(!ChatGPTChatStatus.isDesktopShell(URL(string: "app://else/index.html")!))
        precondition(!ChatGPTChatStatus.isDesktopShell(URL(string: "app://-/fake/index.html")!))
        precondition(!ChatGPTChatStatus.isDesktopShell(URL(string: "app://user@-/index.html")!))
        precondition(ChatGPTChatStatus.currentDesktopLinkID(from: shell, current: "page") == nil,
                     "The launch route must not be mistaken for the active chat")
        let current = URL(string: "app://-/c/\(id.uppercased())")!
        precondition(ChatGPTChatStatus.currentDesktopLinkID(from: current, current: "page") == id)
        precondition(ChatGPTChatStatus.currentDesktopLinkID(from: current, current: nil) == nil)
        precondition(ChatGPTChatStatus.currentDesktopLinkID(from: current, current: "false") == nil)
        for url in ["app://other/c/\(id)", "app://-/thread/\(id)", "app://-/c/%32\(id.dropFirst())",
                    "app://-/c/\(id)/extra", "app://-:1234/c/\(id)", "app://user@-/c/\(id)"] {
            precondition(ChatGPTChatStatus.currentDesktopLinkID(from: URL(string: url)!, current: "page") == nil)
        }
        precondition(ChatGPTChatStatus.displayTitle(from: "\nExample chat – ChatGPT\t") == "Example chat")
        precondition(ChatGPTChatStatus.displayTitle(from: "ChatGPT - Example chat") == "Example chat")
        precondition(ChatGPTChatStatus.displayTitle(from: "ChatGPT") == "ChatGPT chat")
        precondition(ChatGPTChatStatus.displayTitle(from: String(repeating: "é", count: 240)).count == 160)

        let windowTitle = ChatGPTChatStatus.windowChatTitle(windowTitle: " Café   notes - ChatGPT ", pageTitle: "Cafe\u{301} notes")
        precondition(windowTitle == "Café notes", "Only agreeing normalized non-generic titles identify a chat")
        precondition(ChatGPTChatStatus.windowChatTitle(windowTitle: "ChatGPT", pageTitle: "ChatGPT - Café notes") == windowTitle)
        precondition(ChatGPTChatStatus.windowChatTitle(windowTitle: "Café notes", pageTitle: "Different chat") == nil)
        precondition(ChatGPTChatStatus.windowChatTitle(windowTitle: "Home", pageTitle: "New chat") == nil)
        precondition(ChatGPTChatStatus.windowChatTitle(windowTitle: nil, pageTitle: "Settings") == nil)
        precondition(ChatGPTChatStatus.windowChatTitle(windowTitle: "Bad\u{0}Title", pageTitle: nil) == nil)
        let windowID = ChatGPTChatStatus.windowChatIdentifier(windowToken: "window-a", title: windowTitle!)
        precondition(windowID.range(of: #"^window:[0-9a-f]{32}$"#, options: .regularExpression) != nil)
        precondition(windowID == ChatGPTChatStatus.windowChatIdentifier(windowToken: "window-a", title: "Café notes"))
        precondition(windowID != ChatGPTChatStatus.windowChatIdentifier(windowToken: "window-b", title: "Café notes"))
        precondition(windowID != ChatGPTChatStatus.windowChatIdentifier(windowToken: "window-a", title: "Different chat"))

        #if AGENT_BOARD_DIRECT
        let profile = ChatGPTChatStatus.profile
        precondition(profile.bundleID == "com.openai.codex")
        precondition(profile.chatID(URL(string: "https://chatgpt.com/c/\(id)")!) == id)
        precondition(profile.stopLabels.isDisjoint(with: profile.idleLabels))
        precondition(profile.composerLabels.contains("Ask ChatGPT") && profile.stopLabels.contains("Stop")
                     && profile.idleLabels.contains("Send"))
        precondition(profile.shellIdentity?.titleFallback?.composerLabel == "Ask ChatGPT")
        let fallback = profile.shellIdentity!.titleFallback!
        precondition(fallback.acceptsProviderControl(role: "AXButton", label: "Select ChatGPT model",
                                                     enabled: true, hidden: false))
        for label in ["Ask ChatGPT", "Work with ChatGPT", "Ask anything", "Stop", "Send",
                      "Chat actions", "Switch mode, current mode: ChatGPT", "Select Codex model"] {
            precondition(!fallback.acceptsProviderControl(role: "AXButton", label: label,
                                                          enabled: true, hidden: false),
                         "A shared composer or global mode control is not regular ChatGPT identity")
        }
        precondition(!fallback.acceptsProviderControl(role: "AXStaticText", label: "Select ChatGPT model",
                                                      enabled: true, hidden: false))
        precondition(!fallback.acceptsProviderControl(role: "AXButton", label: "Select ChatGPT model",
                                                      enabled: false, hidden: false))
        precondition(!fallback.acceptsProviderControl(role: "AXButton", label: "Select ChatGPT model",
                                                      enabled: true, hidden: true))
        #else
        precondition(!ChatGPTChatStatus.isTrusted && ChatGPTChatStatus.readWithDiagnostics().readings.isEmpty,
                     "Store builds must not inspect other apps")
        #endif
        print("ChatGPT chat identity, title bounds, and source scope passed.")
    }
}
