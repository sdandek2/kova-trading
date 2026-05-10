import Foundation
import Combine

@MainActor
class OrdersViewModel: ObservableObject {
    @Published var orders: [Order] = []
    @Published var isLoading = false
    @Published var errorMessage: String?

    private var cancellables = Set<AnyCancellable>()

    init() {
        WebSocketService.shared.$latestMessage
            .compactMap { $0 }
            .sink { [weak self] message in
                if case .orderFilled(let order) = message {
                    self?.orders.insert(order, at: 0)
                }
            }
            .store(in: &cancellables)
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        do {
            orders = try await APIService.shared.getOrders()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}
