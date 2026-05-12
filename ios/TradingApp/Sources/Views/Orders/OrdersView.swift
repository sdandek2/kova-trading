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
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                if ["accepted", "pending_new", "new", "partially_filled"].contains(order.status) {
                                    Button(role: .destructive) {
                                        Task { await vm.cancelOrder(order) }
                                    } label: {
                                        Label("Cancel", systemImage: "xmark.circle")
                                    }
                                }
                            }
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
