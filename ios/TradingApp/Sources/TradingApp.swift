import SwiftUI

@main
struct KovaApp: App {
    @StateObject private var wsService = WebSocketService.shared
    @AppStorage("appColorScheme") private var colorSchemePreference: String = "system"

    init() {
        NotificationService.shared.requestPermission()
    }

    private var preferredColorScheme: ColorScheme? {
        switch colorSchemePreference {
        case "light": return .light
        case "dark":  return .dark
        default:      return nil   // system
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .preferredColorScheme(preferredColorScheme)
                .onAppear { WebSocketService.shared.connect() }
                .onDisappear { WebSocketService.shared.disconnect() }
        }
    }
}
