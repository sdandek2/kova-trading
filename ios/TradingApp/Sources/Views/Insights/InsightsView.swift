import SwiftUI

struct InsightsView: View {
    @StateObject private var vm = InsightsViewModel()
    @State private var searchText = ""
    @State private var selectedSymbol: String? = nil
    @State private var showPrediction = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    // Search bar for any stock prediction
                    PredictionSearchBar(
                        searchText: $searchText,
                        onSearch: { symbol in
                            selectedSymbol = symbol
                            showPrediction = true
                        }
                    )
                    .padding(.horizontal)

                    // Daily growth picks (top of page — most prominent)
                    DailyPicksView(vm: vm)

                    Divider().padding(.horizontal)

                    // Suggestions section
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text("AI Suggestions")
                                .font(.headline)
                            Spacer()
                            if vm.isLoadingSuggestions {
                                ProgressView().scaleEffect(0.7)
                            } else {
                                Button {
                                    Task { await vm.loadSuggestions() }
                                } label: {
                                    Image(systemName: "arrow.clockwise")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                        .padding(.horizontal)

                        if let error = vm.suggestionsError {
                            Text(error)
                                .font(.caption)
                                .foregroundStyle(.red)
                                .padding(.horizontal)
                        } else if vm.suggestions.isEmpty && !vm.isLoadingSuggestions {
                            Text("Tap refresh to load AI suggestions")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .padding(.horizontal)
                        } else {
                            ForEach(vm.suggestions) { suggestion in
                                SuggestionCard(suggestion: suggestion) {
                                    selectedSymbol = suggestion.symbol
                                    showPrediction = true
                                }
                                .padding(.horizontal)
                            }
                        }
                    }
                }
                .padding(.vertical)
            }
            .navigationTitle("Insights")
            .navigationDestination(isPresented: $showPrediction) {
                if let symbol = selectedSymbol {
                    PredictionDetailView(symbol: symbol)
                }
            }
            .task {
                async let picks: () = vm.dailyPicks == nil ? vm.loadDailyPicks() : ()
                async let sugg: () = vm.suggestions.isEmpty ? vm.loadSuggestions() : ()
                _ = await (picks, sugg)
            }
        }
    }
}

// MARK: - Search Bar

struct PredictionSearchBar: View {
    @Binding var searchText: String
    var onSearch: (String) -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
            TextField("Search any stock (e.g. AAPL, NVDA)", text: $searchText)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.characters)
                .onSubmit {
                    let sym = searchText.trimmingCharacters(in: .whitespaces)
                    if !sym.isEmpty { onSearch(sym) }
                }
            if !searchText.isEmpty {
                Button {
                    let sym = searchText.trimmingCharacters(in: .whitespaces)
                    if !sym.isEmpty { onSearch(sym) }
                } label: {
                    Text("Predict")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.blue)
                        .clipShape(Capsule())
                }
            }
        }
        .padding(10)
        .background(Color(.systemGray6))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}

// MARK: - Suggestion Card

struct SuggestionCard: View {
    let suggestion: Suggestion
    var onTap: () -> Void
    @State private var showTrade = false

    private var suggestedQty: Int {
        guard let price = suggestion.current_price, price > 0 else { return 1 }
        let budget: Double = suggestion.risk_level == "low" ? 1000 : suggestion.risk_level == "high" ? 400 : 700
        return max(1, Int(budget / price))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(suggestion.symbol)
                    .font(.headline)
                Spacer()
                typeChip
                riskChip
            }

                if let price = suggestion.current_price {
                    HStack(spacing: 12) {
                        Text(String(format: "$%.2f", price))
                            .font(.subheadline.weight(.medium))
                        if let chg = suggestion.five_day_change_pct {
                            Text(String(format: "%@%.1f%% 5d", chg >= 0 ? "+" : "", chg))
                                .font(.caption)
                                .foregroundStyle(chg >= 0 ? .green : .red)
                        }
                        if let upside = suggestion.upside_pct {
                            Text(String(format: "↑%.0f%% upside", upside))
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.blue)
                        }
                    }
                }

                horizonBadge

                Text(suggestion.short_term_thesis)
                    .font(.caption)
                    .foregroundStyle(.primary)
                    .lineLimit(2)

                if suggestion.horizon != "short_term" {
                    Text(suggestion.long_term_thesis)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                Text("Entry: \(suggestion.entry_note)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)

                HStack {
                    Text("AI suggests \(suggestedQty) share\(suggestedQty == 1 ? "" : "s")")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button {
                        showTrade = true
                    } label: {
                        Text("Buy")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 6)
                            .background(Color.green)
                            .clipShape(Capsule())
                    }
                }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
        .contentShape(Rectangle())
        .onTapGesture { onTap() }
        .sheet(isPresented: $showTrade) {
            TradeSheet(prefillSymbol: suggestion.symbol, prefillSide: "buy", prefillQty: suggestedQty)
        }
    }

    var typeChip: some View {
        Text(suggestion.type.replacingOccurrences(of: "_", with: " "))
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(typeColor)
            .clipShape(Capsule())
    }

    var riskChip: some View {
        Text(suggestion.risk_level.uppercased())
            .font(.caption2.weight(.bold))
            .foregroundStyle(riskColor)
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .overlay(Capsule().stroke(riskColor, lineWidth: 1))
    }

    var horizonBadge: some View {
        HStack(spacing: 4) {
            Image(systemName: suggestion.horizon == "short_term" ? "clock" : suggestion.horizon == "long_term" ? "calendar" : "arrow.left.and.right")
                .font(.caption2)
            Text(suggestion.horizon.replacingOccurrences(of: "_", with: " ").capitalized)
                .font(.caption2)
        }
        .foregroundStyle(.secondary)
    }

    var typeColor: Color {
        switch suggestion.type {
        case "momentum": return .blue
        case "defensive": return .gray
        case "geopolitical": return .orange
        case "contrarian": return .purple
        default: return .teal
        }
    }

    var riskColor: Color {
        switch suggestion.risk_level {
        case "low": return .green
        case "high": return .red
        default: return .orange
        }
    }
}
