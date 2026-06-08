import UserNotifications
import Combine

class NotificationService {
    static let shared = NotificationService()

    func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    func notifyTrade(side: String, symbol: String, qty: Double, price: Double?) {
        let content = UNMutableNotificationContent()
        content.title = "\(side.uppercased()) \(symbol) Executed"
        let priceText = price.map { " at $\(String(format: "%.2f", $0))" } ?? ""
        content.body = "AI traded \(Int(qty)) shares of \(symbol)\(priceText)"
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }

    func notifyCircuitBreaker(reason: String) {
        let content = UNMutableNotificationContent()
        content.title = "Trading Paused"
        content.body = reason
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: "circuit_breaker",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }

    func notifyConnectorAlert(connector: String, severity: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = severity == "critical" ? "🔴 Connector Critical" : "⚠️ Connector Warning"
        content.body = body
        content.sound = severity == "critical" ? .defaultCritical : .default

        // Use connector name as identifier so repeated alerts replace each other
        let request = UNNotificationRequest(
            identifier: "connector_\(connector)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }
}
