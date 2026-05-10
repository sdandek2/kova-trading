import SwiftUI

struct OrdersView: View {
    @StateObject private var vm = OrdersViewModel()

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.orders.isEmpty {
                    LoadingView()
                } else if let error = vm.errorMessage, vm.orders.isEmpty {
                    ErrorView(message: error) { await vm.load() }
                } else if vm.orders.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "tray")
                            .font(.system(size: 48))
                            .foregroundStyle(.secondary)
                        Text("No Orders")
                            .font(.headline)
                        Text("Orders placed by the AI will appear here.")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .padding()
                } else {
                    List(vm.orders) { order in
                        OrderRowView(order: order)
                    }
                    .listStyle(.insetGrouped)
                    .refreshable { await vm.load() }
                }
            }
            .navigationTitle("Orders")
        }
        .task { await vm.load() }
    }
}
