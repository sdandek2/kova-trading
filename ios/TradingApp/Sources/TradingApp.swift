import SwiftUI

@main
struct KovaApp: App {
    @StateObject private var wsService = WebSocketService.shared

    init() {
        NotificationService.shared.requestPermission()
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .onAppear {
                    WebSocketService.shared.connect()
                }
                .onDisappear {
                    WebSocketService.shared.disconnect()
                }
        }
    }
}
