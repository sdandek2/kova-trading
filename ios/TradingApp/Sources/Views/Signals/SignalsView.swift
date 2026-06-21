import SwiftUI

// Unified Signals tab: Daily Picks · AI Ideas · Market News in one place.

struct SignalsView: View {
    @StateObject private var insightsVM = InsightsViewModel()
    @StateObject private var newsVM    = NewsViewModel()
    // Default to News (index 2) — AI segments only load when explicitly tapped
    @State private var segment   = 2
    @State private var picksLoaded  = false
    @State private var ideasLoaded  = false
    @State private var selectedSymbol: String? = nil
    @State private var showPrediction = false
    @State private var searchText = ""
    @Namespace private var segNS

    private let segments = ["Picks", "Ideas", "News"]
    private let segIcons = ["sparkles", "lightbulb.fill", "newspaper.fill"]

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // ── Custom segment picker ──────────────────────────────────
                HStack(spacing: 6) {
                    ForEach(segments.indices, id: \.self) { i in
                        Button {
                            withAnimation(LakshmiTheme.springSnappy) { segment = i }
                            // Lazy-load AI data only on first explicit tap
                            if i == 0 && !picksLoaded {
                                picksLoaded = true
                                Task { await insightsVM.loadDailyPicks() }
                            }
                            if i == 1 && !ideasLoaded {
                                ideasLoaded = true
                                Task { await insightsVM.loadSuggestions() }
                            }
                        } label: {
                            HStack(spacing: 5) {
                                Image(systemName: segIcons[i])
                                    .font(.system(size: 11, weight: .semibold))
                                Text(segments[i])
                                    .font(.system(size: 12, weight: .bold))
                            }
                            .foregroundStyle(segment == i ? .white : .secondary)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 8)
                            .frame(maxWidth: .infinity)
                            .background {
                                if segment == i {
                                    Capsule()
                                        .fill(LakshmiTheme.brandGradient)
                                        .matchedGeometryEffect(id: "segSel", in: segNS)
                                }
                            }
                        }
                        .buttonStyle(PressScaleButtonStyle(scale: 0.94))
                    }
                }
                .padding(5)
                .background(Color(.tertiarySystemBackground))
                .clipShape(Capsule())
                .padding(.horizontal, 16)
                .padding(.vertical, 10)

                Divider()

                // ── Content ────────────────────────────────────────────────
                switch segment {
                case 0:  PicksPage(vm: insightsVM, selectedSymbol: $selectedSymbol, showPrediction: $showPrediction)
                case 1:  IdeasPage(vm: insightsVM, selectedSymbol: $selectedSymbol, showPrediction: $showPrediction)
                default: NewsPage(vm: newsVM)
                }
            }
            .navigationTitle("Signals")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        Image(systemName: "waveform.path.ecg.rectangle.fill")
                            .foregroundStyle(LakshmiTheme.brandGradient)
                        Text("Signals")
                            .font(.headline.weight(.bold))
                    }
                }
            }
            .navigationDestination(isPresented: $showPrediction) {
                if let sym = selectedSymbol { PredictionDetailView(symbol: sym) }
            }
        }
        .task {
            // Only News loads on open — AI segments load on explicit tap
            if newsVM.articles.isEmpty { await newsVM.load() }
        }
    }
}

// MARK: - Picks page

private struct PicksPage: View {
    @ObservedObject var vm: InsightsViewModel
    @Binding var selectedSymbol: String?
    @Binding var showPrediction: Bool
    @State private var searchText = ""
    @State private var isResolving = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                // ── Search any ticker ──────────────────────────────────────
                TickerSearchRow(searchText: $searchText, isResolving: $isResolving) { sym in
                    selectedSymbol = sym
                    showPrediction = true
                }
                .padding(.horizontal, 16)
                .cardAppear(delay: 0.0)

                DailyPicksView(vm: vm)
                    .cardAppear(delay: 0.05)
            }
            .padding(.top, 12)
            .padding(.bottom, 32)
        }
        .refreshable { await vm.loadDailyPicks(refresh: true) }
    }
}

// MARK: - Ideas page

private struct IdeasPage: View {
    @ObservedObject var vm: InsightsViewModel
    @Binding var selectedSymbol: String?
    @Binding var showPrediction: Bool

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                HStack {
                    Text("AI Suggestions")
                        .font(.headline)
                    Spacer()
                    if vm.isLoadingSuggestions {
                        ProgressView().scaleEffect(0.7)
                    } else {
                        Button { Task { await vm.loadSuggestions(refresh: true) } } label: {
                            Image(systemName: "arrow.clockwise")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.horizontal, 16)

                if let err = vm.suggestionsError {
                    Text(err).font(.caption).foregroundStyle(.red).padding(.horizontal, 16)
                } else if vm.suggestions.isEmpty && !vm.isLoadingSuggestions {
                    VStack(spacing: 10) {
                        Image(systemName: "lightbulb").font(.system(size: 36))
                            .foregroundStyle(LakshmiTheme.gold.opacity(0.4))
                        Text("Tap refresh to load AI suggestions")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 40)
                } else {
                    ForEach(Array(vm.suggestions.enumerated()), id: \.element.id) { idx, sug in
                        SuggestionCard(
                            suggestion: sug,
                            livePrice: vm.livePrices[sug.symbol]
                        ) {
                            selectedSymbol = sug.symbol
                            showPrediction = true
                        }
                        .padding(.horizontal, 16)
                        .cardAppear(delay: Double(idx) * 0.06)
                    }
                }
            }
            .padding(.top, 12)
            .padding(.bottom, 32)
        }
        .refreshable { await vm.loadSuggestions(refresh: true) }
    }
}

// MARK: - News page

private struct NewsPage: View {
    @ObservedObject var vm: NewsViewModel

    var body: some View {
        Group {
            if vm.isLoading && vm.articles.isEmpty {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if vm.articles.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "newspaper").font(.system(size: 40)).foregroundStyle(.secondary)
                    Text("No market news available").foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(vm.articles) { article in
                    Group {
                        if let url = URL(string: article.url), !article.url.isEmpty {
                            Link(destination: url) { NewsRowView(article: article) }.foregroundStyle(.primary)
                        } else {
                            NewsRowView(article: article)
                        }
                    }
                }
                .listStyle(.insetGrouped)
                .refreshable { await vm.load() }
            }
        }
    }
}

// MARK: - Ticker search row

private struct TickerSearchRow: View {
    @Binding var searchText: String
    @Binding var isResolving: Bool
    var onSearch: (String) -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            TextField("Ticker or company (e.g. Apple, NVDA)", text: $searchText)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .onSubmit { Task { await resolve() } }
            if isResolving {
                ProgressView().scaleEffect(0.7)
            } else if !searchText.isEmpty {
                Button { Task { await resolve() } } label: {
                    Text("Predict")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(LakshmiTheme.amber)
                        .clipShape(Capsule())
                }
            }
        }
        .padding(10)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
    }

    private func resolve() async {
        let input = searchText.trimmingCharacters(in: .whitespaces)
        guard !input.isEmpty else { return }
        isResolving = true
        let ticker = await APIService.shared.resolveToTicker(input) ?? input.uppercased()
        isResolving = false
        onSearch(ticker)
    }
}
