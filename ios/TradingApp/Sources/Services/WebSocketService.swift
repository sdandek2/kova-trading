import Foundation
import Combine

enum WSMessage {
    case positionUpdate([Position])
    case orderFilled(Order)
    case aiAnalysis(AIAnalysis)
}

class WebSocketService: ObservableObject {
    static let shared = WebSocketService()

    @Published var latestMessage: WSMessage?

    private var task: URLSessionWebSocketTask?
    private var reconnectDelay: TimeInterval = 1
    private var isConnected = false

    func connect() {
        guard let url = URL(string: Config.websocketURL) else { return }
        task = URLSession.shared.webSocketTask(with: url)
        task?.resume()
        isConnected = true
        reconnectDelay = 1
        receive()
    }

    func disconnect() {
        isConnected = false
        task?.cancel(with: .goingAway, reason: nil)
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                if case .string(let text) = message {
                    self.handle(text)
                }
                self.receive()
            case .failure:
                self.scheduleReconnect()
            }
        }
    }

    private func handle(_ text: String) {
        guard
            let data = text.data(using: .utf8),
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let type = json["type"] as? String,
            let payload = json["data"]
        else { return }

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601

        let payloadData = (try? JSONSerialization.data(withJSONObject: payload)) ?? Data()

        DispatchQueue.main.async {
            switch type {
            case "position_update":
                if let positions = try? decoder.decode([Position].self, from: payloadData) {
                    self.latestMessage = .positionUpdate(positions)
                }
            case "order_filled":
                if let order = try? decoder.decode(Order.self, from: payloadData) {
                    self.latestMessage = .orderFilled(order)
                    NotificationService.shared.notifyTrade(
                        side: order.side,
                        symbol: order.symbol,
                        qty: order.qty,
                        price: order.filledAvgPrice
                    )
                }
            case "ai_analysis":
                if let analysis = try? decoder.decode(AIAnalysis.self, from: payloadData) {
                    self.latestMessage = .aiAnalysis(analysis)
                }
            default:
                break
            }
        }
    }

    private func scheduleReconnect() {
        guard isConnected else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + reconnectDelay) { [weak self] in
            guard let self else { return }
            self.reconnectDelay = min(self.reconnectDelay * 2, 30)
            self.connect()
        }
    }
}
