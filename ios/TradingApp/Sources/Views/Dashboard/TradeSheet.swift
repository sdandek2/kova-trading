import SwiftUI

struct TradeSheet: View {
    @Environment(\.dismiss) private var dismiss
    var prefillSymbol: String = ""
    var prefillSide: String = "buy"
    var prefillQty: Int? = nil

    @State private var symbol: String = ""
    @State private var side: String = "buy"
    @State private var qtyText: String = ""
    @State private var isSubmitting = false
    @State private var errorMessage: String?
    @State private var successMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Stock") {
                    HStack {
                        TextField("Symbol (e.g. AAPL)", text: $symbol)
                            .textInputAutocapitalization(.characters)
                            .autocorrectionDisabled()
                            .font(.headline)
                    }
                }

                Section("Order") {
                    Picker("Side", selection: $side) {
                        Text("Buy").tag("buy")
                        Text("Sell").tag("sell")
                    }
                    .pickerStyle(.segmented)

                    HStack {
                        Text("Shares")
                        Spacer()
                        TextField("Qty", text: $qtyText)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                    }
                }

                if let error = errorMessage {
                    Section {
                        Text(error)
                            .foregroundStyle(.red)
                            .font(.caption)
                    }
                }

                if let success = successMessage {
                    Section {
                        Text(success)
                            .foregroundStyle(.green)
                            .font(.caption)
                    }
                }

                Section {
                    Button {
                        Task { await submitOrder() }
                    } label: {
                        HStack {
                            Spacer()
                            if isSubmitting {
                                ProgressView()
                            } else {
                                Text(side == "buy" ? "Place Buy Order" : "Place Sell Order")
                                    .fontWeight(.semibold)
                                    .foregroundStyle(side == "buy" ? .green : .red)
                            }
                            Spacer()
                        }
                    }
                    .disabled(symbol.isEmpty || qtyText.isEmpty || isSubmitting)
                }
            }
            .navigationTitle("Manual Trade")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .onAppear {
                symbol = prefillSymbol
                side = prefillSide
                if let qty = prefillQty { qtyText = "\(qty)" }
            }
        }
    }

    private func submitOrder() async {
        guard let qty = Int(qtyText), qty > 0 else {
            errorMessage = "Enter a valid quantity"
            return
        }
        isSubmitting = true
        errorMessage = nil
        successMessage = nil
        do {
            try await APIService.shared.placeManualOrder(symbol: symbol.uppercased(), side: side, qty: qty)
            successMessage = "✓ \(side.capitalized) order for \(qty) \(symbol.uppercased()) submitted"
            qtyText = ""
        } catch {
            errorMessage = error.localizedDescription
        }
        isSubmitting = false
    }
}
