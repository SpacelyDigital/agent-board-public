import Foundation

/// Run with ClaudeChatStatus.swift in a temporary build. Does not query any app.
@main
struct ClaudeChatStatusProof {
    static func main() {
        let id = "11111111-1111-4111-8111-111111111111"
        func chatID(_ url: String) -> String? {
            URL(string: url).flatMap(ClaudeChatStatus.chatID(from:))
        }
        precondition(chatID("https://claude.ai/chat/\(id.uppercased())?view=example#footer") == id)
        for url in [
            "http://claude.ai/chat/\(id)", "https://app.claude.ai/chat/\(id)",
            "https://claude.ai.example/chat/\(id)", "https://claude.ai@other.example/chat/\(id)",
            "https://other@claude.ai/chat/\(id)", "https://claude.ai:8443/chat/\(id)",
            "https://claude.ai/project/\(id)", "https://claude.ai/chat/\(id)/extra",
            "https://claude.ai/share/\(id)", "https://claude.ai/chat/new",
            "https://claude.ai/%63hat/\(id)", "https://claude.ai/chat/%31\(id.dropFirst())",
        ] {
            precondition(chatID(url) == nil, "Accepted an unverified chat route: \(url)")
        }

        precondition(ClaudeChatStatus.displayTitle(from: "\n Example conversation  - Claude\t") == "Example conversation")
        precondition(ClaudeChatStatus.displayTitle(from: "Claude") == "Claude chat")
        precondition(ClaudeChatStatus.displayTitle(from: String(repeating: "é", count: 240)).count == 160)

        let classify = { (composer: Bool, stop: Bool, responding: Bool, send: Bool) in
            ClaudeChatStatus.classify(composer: composer, stop: stop, responding: responding,
                                      send: send, idleScopeComplete: true)
        }
        precondition(classify(false, true, true, true) == nil, "Detached controls are not chat evidence")
        precondition(classify(true, false, false, false) == nil, "A composer alone cannot imply completion")
        precondition(classify(true, true, false, true) == .cooking, "Stop overrides Send or voice-mode idle evidence")
        precondition(classify(true, false, true, true) == .cooking, "Responding overrides Send or voice-mode idle evidence")
        precondition(classify(true, false, true, false) == .cooking)
        precondition(classify(true, false, false, true) == .idle)
        precondition(ClaudeChatStatus.classify(composer: true, stop: false, responding: false,
                                               send: true, idleScopeComplete: false) == nil,
                     "Send cannot imply idle when any control scope is incomplete")
        precondition(ClaudeChatStatus.classify(composer: true, stop: true, responding: false,
                                               send: true, idleScopeComplete: false) == .cooking,
                     "Positive Stop evidence does not require a complete idle scan")

        #if AGENT_BOARD_DIRECT
        let profile = DesktopChatAccessibilityReader.Profile.claude
        precondition(profile.composerLabels.contains("Write your prompt to Claude"))
        precondition(profile.idleLabels.contains("Send message"), "The observed finished chat exposes Send message")
        precondition(profile.idleLabels.contains("Use voice mode"), "The finished composer also exposes voice mode")
        precondition(!profile.idleLabels.contains("Dictate"), "Dictate also appears while a response is active")
        precondition(profile.stopLabels.contains("Stop response"), "Active response controls take precedence")
        let complete = DesktopChatAccessibilityReader.completeChildBatch
        precondition(complete(nil, 0, 24, 3), "A successful short child copy does not require a count")
        precondition(complete(nil, 0, 24, 0), "A successful empty child copy proves a leaf")
        precondition(!complete(nil, 0, 24, 24), "A full unknown-count batch can be truncated")
        precondition(!complete(nil, 1, 24, 3), "A tail batch cannot prove the whole scope")
        precondition(complete(24, 0, 24, 24), "A known exact full batch is complete")
        precondition(!complete(25, 1, 24, 24), "A known truncated tail is incomplete")
        precondition(!complete(3, 0, 24, 2), "An inconsistent returned count is incomplete")
        precondition(DesktopChatAccessibilityReader.absentChildValue(.noValue, knownCount: nil))
        precondition(!DesktopChatAccessibilityReader.absentChildValue(.noValue, knownCount: 1))
        precondition(!DesktopChatAccessibilityReader.absentChildValue(.attributeUnsupported, knownCount: nil))
        precondition(!DesktopChatAccessibilityReader.absentChildValue(.cannotComplete, knownCount: nil))
        precondition(!DesktopChatAccessibilityReader.absentChildValue(.failure, knownCount: nil))
        #endif

        #if !AGENT_BOARD_DIRECT
        precondition(!ClaudeChatStatus.isTrusted && !ClaudeChatStatus.requestPermission() && ClaudeChatStatus.read().isEmpty,
                     "Store builds must not inspect other apps or request accessibility access")
        #endif
        print("Claude chat identity, title bounds, and explicit status evidence passed.")
    }
}
