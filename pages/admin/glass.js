const { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } = window.Vue;

// Goose's documented lifecycle is append -> set mode -> configure in a frame.
// Keep at most eight live canvases, and release each one on unmount.
let mountSequence = 0;
let graphicsSupported;
const wallpapers = ['#e6edf8', '#102037'].map((base, index) => {
  const colors = index ? ['#326695', '#544777', '#206665'] : ['#9ccbf7', '#cec0f2', '#a9ddce'];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="960" height="640"><defs>${colors.map((color, n) => `<radialGradient id="g${n}"><stop stop-color="${color}"/><stop offset="1" stop-color="${base}" stop-opacity="0"/></radialGradient>`).join('')}</defs><rect width="960" height="640" fill="${base}"/><ellipse cx="120" cy="120" rx="630" ry="460" fill="url(#g0)"/><ellipse cx="840" cy="260" rx="520" ry="480" fill="url(#g1)"/><ellipse cx="430" cy="630" rx="680" ry="390" fill="url(#g2)"/></svg>`;
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
});
function useGlass(host, mode, configure, update) {
  const ready = ref(false);
  let element = null;
  let disposed = false;
  let observer = null;
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
  onMounted(async () => {
    try {
      if (graphicsSupported === undefined) {
        const probe = document.createElement('canvas').getContext('webgl2');
        graphicsSupported = Boolean(probe);
        probe?.getExtension('WEBGL_lose_context')?.loseContext();
      }
      if (!graphicsSupported) return;
      await Promise.race([customElements.whenDefined('liquid-glass'), new Promise((_, reject) => setTimeout(() => reject(new Error('Glass unavailable')), 5000))]);
      await new Promise(resolve => setTimeout(resolve, (mountSequence++ % 4) * 150));
      if (disposed || !host.value) return;
      element = document.createElement('liquid-glass');
      element.setAttribute('dpr', '2');
      element.style.cssText = 'display:block;width:100%;height:100%';
      // Siri initializes a separate renderer; declare its documented mode up front.
      // Switching from the default glass renderer leaves that library's RAF pending.
      if (mode === 'siri-wave') element.setAttribute('mode', mode);
      const theme = () => {
        if (!element) return;
        const dark = document.documentElement.dataset.theme === 'dark';
        element.toggleAttribute('dark', dark);
        if (mode !== 'siri-wave') element.setAttribute('wallpaper', wallpapers[dark ? 1 : 0]);
      };
      theme();
      host.value.appendChild(element);
      if (mode !== 'siri-wave') element.setAttribute('mode', mode);
      await frame();
      if (disposed) return;
      configure(element);
      observer = new MutationObserver(theme);
      observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
      ready.value = true;
    } catch (_) {
      element?.remove(); element = null;
    }
  });
  onBeforeUnmount(() => { disposed = true; observer?.disconnect(); element?.remove(); element = null; });
  return { ready, sync: async () => { await nextTick(); if (element && ready.value && !disposed) update?.(element); } };
}

const icons = [
  'M3 5a2 2 0 0 1 2-2h6l10 10-8 8L3 11zm4 1a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3',
  'M3 4h18a2 2 0 0 1 2 2v4a2 2 0 0 0 0 4v4a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2v-4a2 2 0 0 0 0-4V6a2 2 0 0 1 2-2m7 4v8h2V8zm5 0v8h2V8z',
  'M12 2a5 5 0 1 1 0 10 5 5 0 0 1 0-10M3 22v-3a7 7 0 0 1 7-5h4a7 7 0 0 1 7 5v3z',
];

const GlassTabs = {
  props: { items: Array, selected: Number, disabled: Boolean, label: String, navigation: Boolean }, emits: ['select'],
  setup(props, { emit }) {
    const host = ref(null);
    const secondRow = props.items.length === 4;
    const stateKey = secondRow ? 'selectedTab2' : 'selectedTab';
    const config = el => {
      const tabs = props.items.map(() => ({ label: '', icon: '' }));
      el.setTabs(secondRow ? [[], tabs] : [tabs]);
      el.setState({ [stateKey]: props.selected });
    };
    const { ready, sync } = useGlass(host, secondRow ? 'bottom-tabs-2' : 'single-bottom-tabs', el => {
      config(el);
      el.addEventListener('lg-statechange', event => {
        const value = event.detail[stateKey];
        if (!Number.isInteger(value) || value < 0 || value >= props.items.length || value === props.selected) return;
        if (props.disabled) sync(); else emit('select', value);
      });
    }, el => el.setState({ [stateKey]: props.selected }));
    watch(() => props.selected, sync);
    const choose = index => { if (!props.disabled && index !== props.selected) emit('select', index); };
    let gesture = null, suppressClick = false, clickTimer;
    const onPointerDown = event => {
      if (ready.value || props.disabled || event.button !== 0 || !event.isPrimary || gesture) return;
      gesture = { id: event.pointerId, x: event.clientX, y: event.clientY, index: Number(event.target.closest('button')?.dataset.index), dragged: false };
      event.currentTarget.setPointerCapture(event.pointerId);
    };
    const onPointerMove = event => {
      if (!gesture || gesture.id !== event.pointerId) return;
      const dx = Math.abs(event.clientX - gesture.x), dy = Math.abs(event.clientY - gesture.y);
      if (dx > 8 && dx > dy) gesture.dragged = true;
      if (gesture.dragged) event.preventDefault();
    };
    const onPointerUp = event => {
      if (!gesture || gesture.id !== event.pointerId) return;
      if (gesture.dragged) {
        const bounds = event.currentTarget.getBoundingClientRect();
        choose(Math.max(0, Math.min(props.items.length - 1, Math.floor((event.clientX - bounds.left) / bounds.width * props.items.length))));
      } else if (Number.isInteger(gesture.index)) choose(gesture.index);
      // Pointer capture retargets clicks to the group; commit only once here.
      suppressClick = true; clearTimeout(clickTimer);
      clickTimer = setTimeout(() => { suppressClick = false; }, 0);
      gesture = null;
    };
    const onClick = (event, index) => { if (!suppressClick || event.detail === 0) choose(index); };
    const cancelGesture = () => { gesture = null; };
    onBeforeUnmount(() => { clearTimeout(clickTimer); gesture = null; });
    const onKey = event => {
      if (props.disabled || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = Number(event.target.closest('button')?.dataset.index);
      if (!Number.isInteger(index)) return;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? props.items.length - 1 : Math.max(0, Math.min(props.items.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)));
      choose(next);
      event.currentTarget.querySelectorAll('button')[next]?.focus();
    };
    return { host, ready, onClick, onKey, onPointerDown, onPointerMove, onPointerUp, cancelGesture };
  },
  // Pointer input reaches Goose's canvas; DOM buttons retain keyboard access.
  template: `<nav class="glass-shell glass-tabs" :class="{fallback:!ready,'is-disabled':disabled}" :aria-label="label"><div ref="host" class="glass-canvas" aria-hidden="true"></div><div class="nav-overlays" :style="{gridTemplateColumns:'repeat('+items.length+',1fr)'}" @keydown="onKey" @pointerdown="onPointerDown" @pointermove="onPointerMove" @pointerup="onPointerUp" @pointercancel="cancelGesture" @lostpointercapture="cancelGesture"><button v-for="(item,index) in items" :key="item.value ?? item.label" :data-index="index" :class="{active:selected===index}" :disabled="disabled" :aria-current="navigation && selected===index?'page':undefined" :aria-pressed="navigation?undefined:selected===index" @click="onClick($event,index)"><svg v-if="item.icon" viewBox="0 0 24 24" aria-hidden="true"><path :d="item.icon"/></svg>{{ item.label }}</button></div></nav>`,
};

export const GlassNav = {
  components: { GlassTabs }, props: { selected: Number, disabled: Boolean }, emits: ['select'],
  setup() { return { items: ['模型定价', '充值卡密', '用户与记录'].map((label, index) => ({ label, icon: icons[index] })) }; },
  template: `<glass-tabs class="glass-nav" :items="items" :selected="selected" :disabled="disabled" label="管理页面" navigation @select="$emit('select',$event)" ></glass-tabs>`,
};

export const GlassSegments = {
  components: { GlassTabs }, props: { items: Array, selected: String, disabled: Boolean, label: String }, emits: ['select'],
  setup(props) {
    return { index: computed(() => Math.max(0, props.items.findIndex(item => item.value === props.selected))) };
  },
  template: `<glass-tabs class="glass-segments" :items="items" :selected="index" :disabled="disabled" :label="label" @select="$emit('select',items[$event].value)" ></glass-tabs>`,
};

export const GlassRing = {
  props: { value: Number },
  setup(props) {
    const host = ref(null);
    // A single SVG arc cannot draw exactly 360 degrees; rounding still shows 100%.
    const config = el => el.setState({ ringProgressValue: Math.min(99.99, props.value) });
    const { ready, sync } = useGlass(host, 'ring-progress', config, config);
    watch(() => props.value, sync);
    return { host, ready };
  },
  template: `<div class="glass-ring" role="img" :aria-label="'有效及已兑换卡密中，已兑换 '+value+'%'"><div ref="host" class="glass-canvas" aria-hidden="true"></div><strong v-if="!ready">{{ value }}%</strong></div>`,
};

export const GlassToggle = {
  props: { value: Boolean, disabled: Boolean }, emits: ['select'],
  setup(props) {
    const host = ref(null);
    const config = el => el.setState({ toggleOn: props.value });
    const { ready, sync } = useGlass(host, 'single-toggle', config, config);
    watch(() => props.value, sync);
    return { host, ready };
  },
  template: `<label class="glass-toggle" :class="{fallback:!ready,on:value}"><div ref="host" class="glass-canvas" aria-hidden="true"></div><input type="checkbox" :checked="value" :disabled="disabled" aria-label="仅显示负余额用户" @change="$emit('select',$event.target.checked)" /></label>`,
};

export const GlassOrb = {
  setup() {
    const host = ref(null);
    const { ready } = useGlass(host, 'siri-wave', el => {
      el.setAttribute('variant', 'orb'); el.setAttribute('scale', '3.5');
      el.setAttribute('speed', matchMedia('(prefers-reduced-motion:reduce)').matches ? '0.01' : '0.55');
    });
    return { host, ready };
  },
  template: `<span class="brand-orb"><div ref="host" class="glass-canvas" aria-hidden="true"></div><span v-if="!ready">A</span></span>`,
};

export const GlassButton = {
  props: { label: String, disabled: Boolean }, emits: ['activate'],
  setup(props, { emit }) {
    const host = ref(null);
    const config = el => el.setButtons([{ id: 'primary', label: props.label, style: 'blue' }]);
    const { ready, sync } = useGlass(host, 'buttons', el => {
      config(el);
      el.addEventListener('lg-buttontap', () => { if (!props.disabled) emit('activate'); });
    }, config);
    watch(() => props.label, sync);
    return { host, ready };
  },
  template: `<div class="glass-shell glass-button" :class="{fallback:!ready,disabled}"><div ref="host" class="glass-canvas" aria-hidden="true"></div><button type="button" :disabled="disabled" @click="$emit('activate')"><span :class="{'sr-only':ready}">{{ label }}</span></button></div>`,
};

export const GlassPreview = {
  props: { tiers: { type: Array, default: () => [] } },
  setup(props) {
    const host = ref(null);
    const config = el => el.setScroll(props.tiers.map(tier => ({ title: tier.condition, subtitle: tier.fees.join(' · ') })));
    const { ready, sync } = useGlass(host, 'scroll-container', config, config);
    watch(() => props.tiers, sync, { deep: true });
    return { host, ready };
  },
  template: `<div class="glass-preview" :class="{fallback:!ready}"><div ref="host" class="preview-artwork" :style="{height:'210px',visibility:ready?'visible':'hidden'}" aria-hidden="true"></div><div class="price-lines"><article class="price-tier" v-for="(tier,index) in tiers" :key="index"><h4>{{ tier.condition }}</h4><p v-for="(fee,n) in tier.fees" :key="n">{{ fee }}</p></article></div></div>`,
};

export const GlassSlider = {
  props: { value: Number }, emits: ['select'],
  setup(props, { emit }) {
    const host = ref(null);
    const { ready, sync } = useGlass(host, 'single-slider', el => {
      el.setState({ sliderValue: props.value });
      el.addEventListener('lg-statechange', event => {
        const value = Math.max(1, Math.min(100, Math.round(event.detail.sliderValue)));
        if (Number.isFinite(value) && value !== props.value) emit('select', value);
      });
    }, el => el.setState({ sliderValue: props.value }));
    watch(() => props.value, sync);
    return { host, ready };
  },
  template: `<div class="glass-shell glass-slider" :class="{fallback:!ready}"><div ref="host" class="glass-canvas" aria-hidden="true"></div><input type="range" min="1" max="100" :value="value" aria-label="生成卡密数量" @input="$emit('select',Number($event.target.value))" /></div>`,
};

export const GlassDialog = {
  props: { title: String, body: String, disabled: Boolean }, emits: ['confirm', 'cancel'],
  setup(props, { emit }) {
    const host = ref(null);
    const { ready } = useGlass(host, 'dialog', el => {
      el.setDialog({ title: props.title, body: props.body, cancelText: '', okayText: '' });
      el.addEventListener('lg-dialogtap', event => {
        if (!props.disabled) emit(event.detail.action === 'okay' ? 'confirm' : 'cancel');
      });
    });
    return { host, ready };
  },
  template: `<div class="glass-shell glass-dialog" :class="{fallback:!ready}"><div ref="host" class="glass-canvas" aria-hidden="true"></div><p :class="ready?'sr-only':''">{{ body }}</p><div class="dialog-buttons"><button type="button" :disabled="disabled" @click="$emit('cancel')">取消</button><button type="button" :disabled="disabled" @click="$emit('confirm')">确认</button></div></div>`,
};
