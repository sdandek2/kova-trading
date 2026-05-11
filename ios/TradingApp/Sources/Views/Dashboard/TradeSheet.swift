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

    // Price & position state
    @State private var currentPrice: Double? = nil
    @State private var isFetchingPrice = false
    @State private var ownedQty: Double? = nil  // shares owned for sell validation

    private var estimatedTotal: Double? {
        guard let price = currentPrice, let qty = Double(qtyText), qty > 0 else { return nil }
        return qty * price
    }

    private var maxSellQty: Int? {
        guard side == "sell", let owned = ownedQty else { return nil }
        return Int(owned)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Stock") {
                    HStack {
                        TextField("Ticker or company (e.g. Apple, AAPL)", text: $symbol)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .font(.headline)
                            .onSubmit { Task { await resolveSymbol() } }
                        if isFetchingPrice {
                            ProgressView()
                                .scaleEffect(0.7)
                        } else if let price = currentPrice {
                            Text(String(format: "$%.2f", price))
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                    }
                    // Hint for company name input (not all-uppercase = needs Return to resolve)
                    let trimmed = symbol.trimmingCharacters(in: .whitespaces)
                    let isResolved = currentPrice != nil
                    let looksLikeName = !trimmed.isEmpty && !isResolved && !isFetchingPrice
                        && !(trimmed.count <= 5 && trimmed == trimmed.uppercased() && !trimmed.contains(" "))
                    if looksLikeName {
                        Text("Press Return ↵ to look up \"\(trimmed)\"")
                            .font(.caption)
                            .foregroundStyle(.blue)
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

                    if let total = estimatedTotal {
                        HStack {
                            Text("Estimated Total")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text(String(format: "~$%.2f", total))
                                .fontWeight(.medium)
                        }
                        .font(.subheadline)
                    }

                    if side == "sell" {
                        if let maxQty = maxSellQty {
                            if maxQty == 0 {
                                Text("You have no shares of \(symbol.uppercased()) to sell.")
                                    .font(.caption)
                                    .foregroundStyle(.orange)
                            } else {
                                Text("You own \(maxQty) share\(maxQty == 1 ? "" : "s") of \(symbol.uppercased()).")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        } else if !symbol.isEmpty {
                            Text("You can only sell shares you own — the order will be rejected if you exceed your position.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
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
                if !symbol.isEmpty {
                    Task { await fetchPriceAndPosition(for: symbol) }
                }
            }
            .onChange(of: symbol) { newSymbol in
                let trimmed = newSymbol.trimmingCharacters(in: .whitespaces)
                guard !trimmed.isEmpty else {
                    currentPrice = nil
                    ownedQty = nil
                    return
                }
                // Only auto-fetch for all-uppercase tickers (e.g. "AAPL", "NVDA").
                // Company names like "apple" or "Tesla" require pressing Return to resolve.
                let looksLikeTicker = trimmed.count <= 5
                    && !trimmed.contains(" ")
                    && trimmed == trimmed.uppercased()
                    && trimmed.allSatisfy({ $0.isLetter || $0.isNumber })
                if looksLikeTicker {
                    Task { await fetchPriceAndPosition(for: trimmed) }
                }
            }
        }
    }

    /// If symbol looks like a company name, resolve it to a ticker first, then fetch price.
    private func resolveSymbol() async {
        let input = symbol.trimmingCharacters(in: .whitespaces)
        guard !input.isEmpty else { return }
        if let ticker = await APIService.shared.resolveToTicker(input), ticker != input.uppercased() {
            symbol = ticker
        }
        await fetchPriceAndPosition(for: symbol.uppercased())
    }

    private func fetchPriceAndPosition(for sym: String) async {
        let upper = sym.uppercased()
        guard !upper.isEmpty else { return }
        isFetchingPrice = true
        defer { isFetchingPrice = false }

        // Fetch current price from predictions endpoint
        if let prediction = try? await APIService.shared.getStockPrediction(symbol: upper) {
            currentPrice = prediction.current_price
        }

        // Fetch owned quantity for sell validation
        if let positions = try? await APIService.shared.getPositions() {
            ownedQty = positions.first(where: { $0.symbol == upper })?.qty ?? 0
        }
    }

    private func submitOrder() async {
        guard let qty = Int(qtyText), qty > 0 else {
            errorMessage = "Enter a valid quantity"
            return
        }

        // Warn if trying to sell more than owned
        if side == "sell", let maxQty = maxSellQty, qty > maxQty {
            errorMessage = "You only own \(maxQty) share\(maxQty == 1 ? "" : "s") of \(symbol.uppercased()). Reduce quantity or the order will be rejected."
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
