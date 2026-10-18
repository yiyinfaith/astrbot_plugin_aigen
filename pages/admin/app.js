import { GlassNav, GlassSegments, GlassButton, GlassSlider, GlassDialog, GlassRing, GlassToggle, GlassOrb } from './glass.js?v=1.4.8';

const { createApp } = window.Vue;
const payloadOf = result => result?.status === 'ok' && 'data' in result ? result.data : result;
const snapshot = row => JSON.stringify([row.model.trim(), row.expression]);

const app = createApp({
  components: { GlassNav, GlassSegments, GlassButton, GlassSlider, GlassDialog, GlassRing, GlassToggle, GlassOrb },
  data() {
    return {
      bridge: null, loading: true, loaded: false, busy: false, error: '', notice: '', tab: 0,
      overview: { models: 0, users: 0, categories: [], charged: '0', running: 0 },
      priceRows: [], priceRevision: 0, priceSearch: '', draft: { id: null, model: '', expression: '0' },
      originalDraft: JSON.stringify(['', '0']), preview: { valid: true, tiers: [], table: null }, validating: false, previewSequence: 0, previewTimer: null,
      cards: { rows: [], total: 0 }, selectedCodes: [], cardFilter: { page: 1, status: 'active', denomination: '', search: '' },
      cardSequence: 0, recordSequence: 0, cardsLoading: false, recordsLoading: false,
      records: { rows: [], total: 0 }, recordKind: 'users', recordFilter: { page: 1, search: '' }, negativeOnly: false,
      cardDraft: { amount: '10', count: 10 }, generated: { amount: '', codes: [] },
      balanceDraft: { platform: 'aiocqhttp', user_id: '', expected: '0', amount: '0', reason: '', existing: false },
      modal: null, pendingConfirmation: null, retryKeys: new Map(), toastTimer: null, offContext: null,
      cardStates: [{ value: 'active', label: '有效' }, { value: 'redeemed', label: '已兑换' }, { value: 'deleted', label: '已删除' }, { value: 'all', label: '全部' }],
      recordKinds: [{ value: 'users', label: '用户余额' }, { value: 'redemptions', label: '兑换记录' }, { value: 'orders', label: '生成账单' }, { value: 'audit', label: '管理员操作' }],
      examples: [
        { name: '按次', expression: 'tier("base", fixed(0.2))' },
        { name: '按成品秒数', expression: 'tier("base", u("actual_seconds") * 0.1)' },
        { name: 'AutoDL 按秒', expression: 'tier("base", u("seconds") * 0.1)' },
        { name: '按 Token', expression: 'tier("base", p * 2 + c * 8)' },
        { name: '画质阶梯', expression: 'u("resolution") == "768p" ? tier("high", u("seconds") * 0.2) : tier("base", u("seconds") * 0.1)' },
        { name: '时段价格', expression: 'hour("Asia/Shanghai") < 8 ? tier("off_peak", u("seconds") * 0.03) : tier("peak", u("seconds") * 0.04)' },
      ],
    };
  },
  computed: {
    activeCards() { return this.overview.categories.reduce((sum, group) => sum + group.active, 0); },
    redeemedCards() { return this.overview.categories.reduce((sum, group) => sum + group.redeemed, 0); },
    redemptionPercent() { const total = this.activeCards + this.redeemedCards; return total ? Math.round(this.redeemedCards / total * 100) : 0; },
    filteredPrices() { return this.priceRows.filter(row => String(row.model).toLowerCase().includes(this.priceSearch.toLowerCase())); },
    dirty() { return snapshot(this.draft) !== this.originalDraft; },
    allCardsSelected() { const codes = this.cards.rows.filter(row => !row.redeemed_by).map(row => row.code); return codes.length > 0 && codes.every(code => this.selectedCodes.includes(code)); },
  },
  methods: {
    pages(total) { return Math.max(1, Math.ceil(total / 30)); },
    async get(action, params = {}) { return payloadOf(await this.bridge.apiGet('finance/' + action, params)); },
    async post(action, body) { return payloadOf(await this.bridge.apiPost('finance/' + action, body)); },
    async mutate(action, body) {
      const key = action + JSON.stringify(body);
      let id = this.retryKeys.get(key);
      if (!id) { id = globalThis.crypto?.randomUUID?.() || 'aigen-' + Date.now() + '-' + Math.random().toString(36).slice(2); this.retryKeys.set(key, id); }
      const result = await this.post(action, { ...body, operation_id: id });
      this.retryKeys.delete(key);
      return result;
    },
    showNotice(text) { this.notice = text; clearTimeout(this.toastTimer); this.toastTimer = setTimeout(() => { this.notice = ''; }, 4500); },
    async run(action) {
      if (this.busy || !this.loaded) return;
      this.busy = true; this.error = '';
      try { await action(); } catch (error) { this.error = error.message || String(error); }
      finally { this.busy = false; }
    },
    async bootstrap() {
      try {
        const bridge = window.AstrBotPluginPage;
        if (!bridge) throw new Error('请从 AstrBot 插件详情页打开 AIGen 管理中心。');
        const context = await Promise.race([bridge.ready(), new Promise((_, reject) => setTimeout(() => reject(new Error('管理页面连接超时，请刷新。')), 12000))]);
        this.bridge = bridge;
        const applyTheme = value => { document.documentElement.dataset.theme = value?.isDark ? 'dark' : 'light'; };
        applyTheme(context); this.offContext = bridge.onContext?.(applyTheme);
        await this.loadAll(); this.loaded = true;
      } catch (error) { this.error = error.message || String(error); }
      finally { this.loading = false; }
    },
    async loadAll() {
      const [overview, prices] = await Promise.all([this.get('overview'), this.get('prices')]);
      this.overview = overview; this.priceRows = prices.rows; this.priceRevision = prices.revision;
      const current = prices.rows.find(row => row.id === this.draft.id) || prices.rows[0];
      if (current) this.selectPrice(current, true); else this.newPrice(true);
      if (this.tab === 1) await this.loadCards();
      if (this.tab === 2) await this.loadRecords();
    },
    refreshAll(force = false) {
      if (this.dirty && !force) { this.confirm('刷新数据', '刷新将放弃尚未保存的价格编辑。', () => this.refreshAll(true)); return; }
      return this.run(async () => { await this.loadAll(); this.showNotice('已刷新最新数据。'); });
    },
    selectPrice(row, force = false) {
      if (this.dirty && !force) { this.confirm('切换模型', '切换模型将放弃尚未保存的价格编辑。', () => this.selectPrice(row, true)); return; }
      this.draft = { id: row.id, model: String(row.model || ''), expression: String(row.expression || '') };
      this.originalDraft = snapshot(this.draft); this.preview = { ...row }; this.schedulePreview();
    },
    newPrice(force = false) {
      if (this.dirty && !force) { this.confirm('添加模型', '添加新模型将放弃尚未保存的价格编辑。', () => this.newPrice(true)); return; }
      this.draft = { id: null, model: '', expression: '0' }; this.originalDraft = snapshot(this.draft); this.schedulePreview();
    },
    useExample(example) { this.draft.expression = example.expression; this.schedulePreview(); },
    schedulePreview() {
      clearTimeout(this.previewTimer); const sequence = ++this.previewSequence; this.validating = true;
      this.previewTimer = setTimeout(async () => {
        try {
          const result = await this.post('preview', { ...this.draft });
          if (sequence === this.previewSequence) this.preview = result;
        } catch (error) {
          if (sequence === this.previewSequence) this.preview = { valid: false, detail: error.message || String(error), tiers: [] };
        } finally { if (sequence === this.previewSequence) this.validating = false; }
      }, 350);
    },
    savePrice() {
      if (!this.draft.model.trim()) { this.error = '请填写模型 ID。'; return; }
      return this.run(async () => {
        const rows = this.priceRows.map(row => ({ id: row.id, model: row.model, expression: row.expression }));
        const position = rows.findIndex(row => row.id === this.draft.id);
        if (position >= 0) rows[position] = { ...this.draft }; else rows.push({ ...this.draft });
        const selectedModel = this.draft.model.trim();
        await this.mutate('save-prices', { revision: this.priceRevision, rows });
        const [prices, overview] = await Promise.all([this.get('prices'), this.get('overview')]);
        this.priceRows = prices.rows; this.priceRevision = prices.revision; this.overview = overview;
        this.selectPrice(prices.rows.find(row => row.model === selectedModel), true);
        this.showNotice('模型价格已保存，下一次生成立即生效。');
      });
    },
    requestDeletePrice() {
      const id = this.draft.id, model = this.draft.model;
      this.confirm('删除模型价格', '删除 ' + model + ' 的价格规则后，该模型将免费。', () => this.run(async () => {
        const rows = this.priceRows.filter(row => row.id !== id).map(row => ({ id: row.id, model: row.model, expression: row.expression }));
        await this.mutate('save-prices', { revision: this.priceRevision, rows });
        await this.loadAll(); this.showNotice('已删除模型价格。');
      }));
    },
    async changeTab(tab) {
      if (!Number.isInteger(tab) || tab < 0 || tab > 2 || this.busy) return;
      this.tab = tab;
      try { if (tab === 1) await this.loadCards(); if (tab === 2) await this.loadRecords(); }
      catch (error) { this.error = error.message || String(error); }
    },
    async loadCards() {
      const sequence = ++this.cardSequence;
      this.cardsLoading = true; this.selectedCodes = []; this.cards = { rows: [], total: 0 };
      try { const result = await this.get('cards', { ...this.cardFilter }); if (sequence === this.cardSequence) { this.cards = result; this.selectedCodes = []; } }
      catch (error) { if (sequence === this.cardSequence) this.error = error.message || String(error); }
      finally { if (sequence === this.cardSequence) this.cardsLoading = false; }
    },
    chooseCardState(value) { if (this.busy || value === this.cardFilter.status) return; this.cardFilter.status = value; this.cardFilter.page = 1; return this.loadCards(); },
    chooseDenomination(amount) { if (this.busy) return; this.cardFilter.denomination = amount; this.cardFilter.page = 1; this.run(() => this.loadCards()); },
    toggleAllCards(event) { this.selectedCodes = event.target.checked ? this.cards.rows.filter(row => !row.redeemed_by).map(row => row.code) : []; },
    cardState(row) { return !row.redeemed_by ? { label: '有效', class: 'green' } : ['[revoked]', '[deleted]'].includes(row.redeemed_by) ? { label: '已删除', class: '' } : { label: '已兑换', class: '' }; },
    openCreateCards() { this.modal = { kind: 'create', title: '生成充值卡密' }; },
    closeModal() { if (!this.busy) { this.modal = null; this.pendingConfirmation = null; } },
    createCards() {
      return this.run(async () => {
        this.generated = await this.mutate('create-cards', { amount: this.cardDraft.amount, count: this.cardDraft.count });
        this.modal = { kind: 'generated', title: '卡密已生成' };
        this.overview = await this.get('overview'); await this.loadCards();
        this.showNotice('卡密已加入面额 ' + this.generated.amount + ' 分类。');
      });
    },
    requestDeleteCards() {
      if (this.busy || this.cardsLoading || !this.selectedCodes.length) return;
      const codes = [...this.selectedCodes];
      this.confirm('删除有效卡密', '将删除选中的 ' + codes.length + ' 张有效卡密，使其无法再兑换。已兑换记录会保留。', () => this.run(async () => {
        await this.mutate('delete-cards', { codes }); this.overview = await this.get('overview'); await this.loadCards(); this.showNotice('选中的卡密已删除。');
      }));
    },
    exportCodes() { if (!this.cardsLoading) this.downloadCodes(this.selectedCodes, 'aigen-active-cards.txt'); },
    downloadCodes(codes, filename) {
      const url = URL.createObjectURL(new Blob([codes.join('\n') + '\n'], { type: 'text/plain;charset=utf-8' }));
      const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 3000);
    },
    async loadRecords() {
      const sequence = ++this.recordSequence;
      this.recordsLoading = true; this.records = { rows: [], total: 0 };
      try { const result = await this.get(this.recordKind, { ...this.recordFilter, status: this.recordKind === 'users' && this.negativeOnly ? 'negative' : 'all' }); if (sequence === this.recordSequence) this.records = result; }
      catch (error) { if (sequence === this.recordSequence) this.error = error.message || String(error); }
      finally { if (sequence === this.recordSequence) this.recordsLoading = false; }
    },
    changeRecordKind(value) { if (this.busy) return; this.recordKind = value; this.recordFilter.page = 1; this.records = { rows: [], total: 0 }; return this.loadRecords(); },
    toggleNegative(value) { this.negativeOnly = value; this.recordFilter.page = 1; return this.run(() => this.loadRecords()); },
    openBalance(row) {
      this.balanceDraft = { platform: row?.platform || 'aiocqhttp', user_id: row?.user_id || '', expected: row?.amount || '0', amount: row?.amount || '0', reason: '', existing: Boolean(row) };
      this.modal = { kind: 'balance', title: '修改用户余额' };
    },
    setBalance() {
      return this.run(async () => {
        const { existing, ...body } = this.balanceDraft;
        await this.mutate('set-balance', body); this.modal = null;
        this.overview = await this.get('overview'); await this.loadRecords(); this.showNotice('用户余额已更新，操作记录已保存。');
      });
    },
    confirm(title, body, action) { this.pendingConfirmation = action; this.modal = { kind: 'confirm', title, body }; },
    async confirmModal() { const action = this.pendingConfirmation; this.modal = null; this.pendingConfirmation = null; await action?.(); },
    orderState(value) { return { running: '生成中', settled: '已扣费', refunded: '未扣费 / 已退款', review: '待核对', reserved: '旧订单' }[value] || value; },
    auditKind(value) { return { balance: '余额调整', create_cards: '生成卡密', delete_cards: '删除卡密', prices: '修改价格' }[value] || value; },
    auditDetail(row) { const value = row.result || {}; return row.kind === 'balance' ? value.owner + '：' + value.before + ' → ' + value.after + '；' + value.reason : row.kind === 'create_cards' ? value.count + ' 张 · 面额 ' + value.amount : row.kind === 'delete_cards' ? '删除 ' + value.deleted + ' 张' : value.models + ' 个模型 · 版本 ' + value.revision; },
  },
  mounted() { this.bootstrap(); },
  beforeUnmount() { clearTimeout(this.previewTimer); clearTimeout(this.toastTimer); this.offContext?.(); },
});
app.mount('#app');
