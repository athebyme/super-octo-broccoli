/* Seller-scoped image lab UI. All HTML rendering uses Alpine text bindings. */
function imageLabLoginUrl() {
  const next = `${window.location.pathname}${window.location.search}`;
  return `/login?next=${encodeURIComponent(next)}`;
}

function redirectImageLabToLogin() {
  if (window.__IMAGE_LAB_AUTH_REDIRECTING) return;
  window.__IMAGE_LAB_AUTH_REDIRECTING = true;
  window.setTimeout(() => window.location.assign(imageLabLoginUrl()), 0);
}

async function readImageLabApiResponse(response) {
  const responseUrl = new URL(response.url || window.location.href, window.location.origin);
  if (responseUrl.pathname === '/login') {
    redirectImageLabToLogin();
    throw new Error('Сессия истекла. Перенаправляем на вход…');
  }

  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  const body = await response.text();
  let data = null;
  if (contentType.includes('json')) {
    try {
      data = JSON.parse(body);
    } catch (_) {
      throw new Error(`Сервер вернул повреждённый JSON (HTTP ${response.status})`);
    }
  } else {
    if (response.status === 401) {
      redirectImageLabToLogin();
      throw new Error('Сессия истекла. Перенаправляем на вход…');
    }
    if (response.status === 400) {
      throw new Error('Страница устарела или защитный токен истёк. Обновите страницу перед повтором');
    }
    if (response.status === 403) {
      throw new Error('Нет доступа к этой операции');
    }
    if ([502, 503, 504].includes(response.status)) {
      throw new Error(`Сервис перезапускается (HTTP ${response.status}). Обновите страницу через несколько секунд`);
    }
    if (response.status === 413) {
      throw new Error('Файл слишком большой для загрузки');
    }
    if (response.status >= 500) {
      throw new Error(`Ошибка сервера (HTTP ${response.status}). Запрос не повторён автоматически`);
    }
    throw new Error(`Сервер вернул неожиданный ответ (HTTP ${response.status})`);
  }

  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error(`Некорректный ответ сервера (HTTP ${response.status})`);
  }
  if (response.status === 401 || data.code === 'auth_required') {
    redirectImageLabToLogin();
    throw new Error(data.error || 'Сессия истекла. Перенаправляем на вход…');
  }
  if (!response.ok || !data.success) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function imageLab() {
  const boot = window.IMAGE_LAB_BOOTSTRAP || {};
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  return {
    products: boot.products || [],
    recommendations: boot.recommendations || [],
    capabilities: boot.capabilities || {backends: [], scenes: []},
    experiments: boot.experiments || [],
    analytics: boot.analytics || {total: 0, variants: []},
    ratingTags: boot.ratingTags || [],
    variants: [], selectedTargetKeys: [], productId: '', marketplaceTargetListingId: '',
    sceneKey: 'luxury', customScene: '',
    additionalPrompt: '', generationMode: 'single', generationStrategy: 'native_scene',
    angleViews: boot.capabilities?.angle_views || [],
    requestedViews: ['front', 'back', 'three_quarter_right'],
    selectedPhotoIndices: [], activePhotoIndex: 0, photoRoles: {},
    includeProductContext: false, useOverlay: false, overlayTitle: '', overlaySubtitle: '',
    useWatermark: false, watermark: {}, watermarkPosition: 'bottom_right',
    watermarkScale: 18, watermarkOpacity: 80,
    sourceLoadFailed: false, sourceRevision: 0,
    blindMode: true, showAnalytics: false, qualityFilter: 'all', productFilter: 'all',
    submitting: false, message: '', messageType: 'success', viewMode: {}, ratingDrafts: {}, timer: null,

    init() {
      this.variants = this.capabilities.backends.flatMap(backend =>
        backend.models.map(model => ({
          key: `${backend.id}:${model.id}`, backend: backend.id, model: model.id,
          label: model.label || model.id, providerLabel: backend.label,
          enabled: backend.enabled, cost_rub: model.cost_rub,
          resolution: model.resolution || '',
          quality: model.quality || '',
          profileLabel: model.profile_label || model.resolution || model.quality || 'Авто',
          max_references: Number(model.max_references || 0),
          supported_strategies: model.supported_strategies || ['background_only'],
          supports_reference: Boolean(model.supports_reference)
        }))
      );
      this.selectedTargetKeys = this.defaultTargetKeys();
      const query = new URLSearchParams(window.location.search);
      const requestedListing = Number(query.get('listing_id'));
      const listingProduct = this.products.find(product =>
        (product.marketplace_targets || []).some(target => Number(target.listing_id) === requestedListing)
      );
      const requestedProduct = Number(query.get('product_id'));
      const requestedExists = this.products.some(item => Number(item.id) === requestedProduct);
      if (listingProduct) {
        this.productId = listingProduct.id;
        this.marketplaceTargetListingId = String(requestedListing);
      } else if (requestedExists) this.productId = requestedProduct;
      else if (this.products.length) this.productId = this.products[0].id;
      this.onProductChange();
      this.experiments = this.shuffle(this.experiments);
      this.timer = window.setInterval(() => this.pollActive(), 2200);
    },
    defaultTargetKeys() {
      const target = this.capabilities?.policy?.default_target || {};
      const preferredKey = target.backend && target.model
        ? `${target.backend}:${target.model}` : '';
      const preferred = this.variants.find(item =>
        item.key === preferredKey && this.variantAvailable(item));
      const fallback = this.variants.find(item => this.variantAvailable(item));
      const selected = preferred || fallback;
      return selected ? [selected.key] : [];
    },
    get currentProduct() {
      return this.products.find(item => Number(item.id) === Number(this.productId)) || null;
    },
    get currentMarketplaceTargets() { return this.currentProduct?.marketplace_targets || []; },
    get currentMarketplaceTarget() {
      return this.currentMarketplaceTargets.find(
        item => Number(item.listing_id) === Number(this.marketplaceTargetListingId)
      ) || null;
    },
    get currentPhotos() { return this.currentProduct?.photos || []; },
    get photoSelectionValid() {
      if (this.generationMode === 'single') return this.selectedPhotoIndices.length === 1;
      if (['collage','reference_set'].includes(this.generationMode)) return this.selectedPhotoIndices.length >= 2;
      return this.selectedPhotoIndices.length >= 1;
    },
    get canGenerate() {
      return Boolean(this.productId && this.selectedTargetKeys.length && this.photoSelectionValid &&
        this.selectedTargetKeys.every(key => {
          const variant = this.variants.find(item => item.key === key);
          return variant && this.variantAvailable(variant);
        }) &&
        (this.generationMode !== 'angles' || this.requestedViews.length) &&
        (!this.useWatermark || this.watermark.id) && (!this.useOverlay || this.overlayTitle.trim()));
    },
    get plannedJobs() {
      const multiplier = this.generationMode === 'each'
        ? this.selectedPhotoIndices.length
        : (this.generationMode === 'angles' ? this.requestedViews.length : 1);
      return this.selectedTargetKeys.length * multiplier;
    },
    get plannedCost() {
      const base = this.selectedTargetKeys.reduce((sum, key) => {
        const item = this.variants.find(value => value.key === key);
        return sum + Number(item?.cost_rub || 0);
      }, 0);
      const multiplier = this.generationMode === 'each'
        ? this.selectedPhotoIndices.length
        : (this.generationMode === 'angles' ? this.requestedViews.length : 1);
      return base * multiplier;
    },
    get generateButtonLabel() {
      if (!this.photoSelectionValid && ['collage','reference_set'].includes(this.generationMode)) return 'Выберите минимум 2 фото';
      if (this.useWatermark && !this.watermark.id) return 'Загрузите PNG-логотип';
      if (this.useOverlay && !this.overlayTitle.trim()) return 'Введите заголовок';
      if (this.generationMode === 'angles' && !this.requestedViews.length) return 'Выберите целевые ракурсы';
      if (!this.photoSelectionValid) return 'Выберите фото';
      return this.plannedJobs > 1 ? `Запустить ${this.plannedJobs} задач` : 'Сгенерировать';
    },
    get filteredExperiments() {
      return this.experiments.filter(item => {
        const quality = item.quality?.status || '';
        if (this.qualityFilter !== 'all' && quality !== this.qualityFilter) return false;
        if (this.productFilter === 'current' && Number(item.product_id) !== Number(this.productId)) return false;
        return true;
      });
    },
    shuffle(values) { return [...values].sort(() => Math.random() - 0.5); },
    variantAvailable(variant) {
      if (!variant.enabled || !variant.supported_strategies.includes(this.generationStrategy)) return false;
      if (this.generationStrategy === 'background_only') return true;
      const referenceCount = ['single','each'].includes(this.generationMode)
        ? 1 : this.selectedPhotoIndices.length;
      return referenceCount <= variant.max_references;
    },
    toggleTarget(variant) {
      if (!this.variantAvailable(variant)) return;
      const index = this.selectedTargetKeys.indexOf(variant.key);
      if (index >= 0) this.selectedTargetKeys.splice(index, 1);
      else if (this.selectedTargetKeys.length < 3) this.selectedTargetKeys.push(variant.key);
      else this.showMessage('Можно сравнить максимум 3 варианта за раз', 'error');
    },
    targets() {
      return this.selectedTargetKeys.map(key => this.variants.find(item => item.key === key))
        .filter(Boolean).map(item => ({backend: item.backend, model: item.model}));
    },
    onProductChange() {
      if (!this.currentMarketplaceTarget) this.marketplaceTargetListingId = '';
      const first = Number(this.currentPhotos[0]?.index || 0);
      this.activePhotoIndex = first;
      this.photoRoles = Object.fromEntries(
        this.currentPhotos.map(item => [Number(item.index), 'angle'])
      );
      this.sourceLoadFailed = false;
      this.sourceRevision += 1;
      if (this.generationMode === 'single') this.selectedPhotoIndices = this.currentPhotos.length ? [first] : [];
      else this.selectedPhotoIndices = this.currentPhotos.map(item => Number(item.index));
      this.fillOverlayFromProduct();
    },
    openRecommendation(item) {
      const product = this.products.find(value => Number(value.id) === Number(item.product_id));
      if (!product) {
        this.showMessage('Товар пока недоступен в Фотостудии: проверьте исходное фото', 'error');
        return;
      }
      this.productId = product.id;
      this.marketplaceTargetListingId = '';
      this.onProductChange();
      if (item.listing_id && this.currentMarketplaceTargets.some(
        target => Number(target.listing_id) === Number(item.listing_id)
      )) this.marketplaceTargetListingId = String(item.listing_id);
      document.getElementById('image-lab-source')?.scrollIntoView({behavior:'smooth', block:'start'});
      this.showMessage('Товар выбран. Проверьте исходники и стоимость перед запуском', 'success');
    },
    async reviewRecommendation(item, status) {
      try {
        await this.api(`/image-lab/api/recommendations/${Number(item.id)}/status`, {
          method:'POST', body:JSON.stringify({status})
        });
        this.recommendations = this.recommendations.filter(value => Number(value.id) !== Number(item.id));
        this.showMessage(status === 'completed' ? 'Рекомендация отмечена выполненной' : 'Рекомендация скрыта', 'success');
      } catch (error) { this.showMessage(error.message, 'error'); }
    },
    onModeChange() {
      if (this.generationMode === 'angles') this.generationStrategy = 'angle_synthesis';
      else if (this.generationStrategy === 'angle_synthesis') this.generationStrategy = 'native_scene';
      if (this.generationMode === 'reference_set' &&
          !['reference_guided','native_scene'].includes(this.generationStrategy)) {
        this.generationStrategy = 'native_scene';
      }
      if (this.generationMode === 'collage' && this.generationStrategy === 'native_scene') {
        this.generationStrategy = 'background_only';
      }
      if (this.generationMode === 'single') {
        this.selectedPhotoIndices = this.currentPhotos.length ? [this.activePhotoIndex] : [];
      } else if (!this.selectedPhotoIndices.length ||
                 (['collage','reference_set'].includes(this.generationMode) && this.selectedPhotoIndices.length < 2)) {
        this.selectedPhotoIndices = this.currentPhotos.map(item => Number(item.index));
      }
      if (!this.selectedPhotoIndices.includes(this.activePhotoIndex) && this.selectedPhotoIndices.length) {
        this.activePhotoIndex = this.selectedPhotoIndices[0];
      }
      this.onStrategyChange();
    },
    onStrategyChange() {
      if (this.generationStrategy === 'background_only' && this.generationMode === 'reference_set') {
        this.generationMode = 'single';
        this.selectedPhotoIndices = this.currentPhotos.length ? [this.activePhotoIndex] : [];
      }
      if (this.generationStrategy === 'native_scene' && this.generationMode === 'collage') {
        this.generationMode = 'single';
        this.selectedPhotoIndices = this.currentPhotos.length ? [this.activePhotoIndex] : [];
      }
      this.selectedTargetKeys = this.selectedTargetKeys.filter(key => {
        const variant = this.variants.find(item => item.key === key);
        return variant && this.variantAvailable(variant);
      });
      if (!this.selectedTargetKeys.length) {
        this.selectedTargetKeys = this.defaultTargetKeys();
      }
    },
    isPhotoSelected(index) { return this.selectedPhotoIndices.includes(Number(index)); },
    togglePhoto(index) {
      const value = Number(index);
      const wasActive = this.activePhotoIndex === value;
      if (this.generationMode !== 'single' && this.isPhotoSelected(value) && !wasActive) {
        this.activePhotoIndex = value; this.sourceLoadFailed = false; return;
      }
      this.activePhotoIndex = value;
      this.sourceLoadFailed = false;
      if (this.generationMode === 'single') {
        this.selectedPhotoIndices = [value];
        return;
      }
      const position = this.selectedPhotoIndices.indexOf(value);
      if (position >= 0) this.selectedPhotoIndices.splice(position, 1);
      else this.selectedPhotoIndices.push(value);
      if (!this.selectedPhotoIndices.length) this.selectedPhotoIndices = [value];
      if (!this.selectedPhotoIndices.includes(this.activePhotoIndex)) {
        this.activePhotoIndex = this.selectedPhotoIndices[0];
      }
      this.selectedPhotoIndices.sort((a, b) => a - b);
    },
    selectAllPhotos() {
      this.selectedPhotoIndices = this.currentPhotos.map(item => Number(item.index));
      if (this.generationMode === 'single' && this.selectedPhotoIndices.length > 1) {
        this.generationMode = 'reference_set';
        if (!['reference_guided','native_scene'].includes(this.generationStrategy)) {
          this.generationStrategy = 'native_scene';
        }
      }
    },
    selectOnlyActive() {
      if (this.generationMode === 'angles') {
        this.selectedPhotoIndices = this.currentPhotos.length ? [this.activePhotoIndex] : [];
        return;
      }
      this.generationMode = 'single';
      if (this.generationStrategy === 'angle_synthesis') this.generationStrategy = 'native_scene';
      this.selectedPhotoIndices = this.currentPhotos.length ? [this.activePhotoIndex] : [];
    },
    retrySource() { this.sourceLoadFailed = false; this.sourceRevision += 1; },
    fillOverlayFromProduct() {
      this.overlayTitle = this.currentProduct?.suggested_overlay?.title || '';
      this.overlaySubtitle = this.currentProduct?.suggested_overlay?.subtitle || '';
    },
    async uploadWatermark(event) {
      const file = event.target.files?.[0];
      if (!file) return;
      const form = new FormData(); form.append('file', file);
      try {
        const response = await fetch('/image-lab/api/watermarks', {
          method:'POST', headers:{'X-CSRFToken':csrf()}, body:form
        });
        const data = await readImageLabApiResponse(response);
        this.watermark = data.watermark; this.useWatermark = true;
        this.showMessage('PNG-логотип загружен', 'success');
      } catch (error) { this.showMessage(error.message, 'error'); }
      finally { event.target.value = ''; }
    },
    async api(url, options = {}) {
      const response = await fetch(url, {
        ...options,
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf(), ...(options.headers || {})}
      });
      return readImageLabApiResponse(response);
    },
    async generate() {
      if (!this.canGenerate || this.submitting) return;
      this.submitting = true; this.message = '';
      try {
        const data = await this.api('/image-lab/api/experiments', {
          method: 'POST', body: JSON.stringify({
            product_id: Number(this.productId), scene_key: this.sceneKey,
            custom_scene: this.customScene, targets: this.targets(),
            additional_prompt: this.additionalPrompt,
            photo_indices: [...this.selectedPhotoIndices], generation_mode: this.generationMode,
            generation_strategy: this.generationStrategy,
            requested_views: this.generationMode === 'angles' ? [...this.requestedViews] : [],
            primary_photo_index: Number(this.activePhotoIndex),
            photo_roles: this.selectedPhotoIndices.map(index => ({
              index:Number(index), role:this.photoRoles[index] || 'angle'
            })),
            include_product_context: Boolean(this.includeProductContext),
            overlay: this.useOverlay ? {
              title:this.overlayTitle, subtitle:this.overlaySubtitle
            } : null,
            watermark: this.useWatermark ? {
              id:this.watermark.id, position:this.watermarkPosition,
              scale_percent:Number(this.watermarkScale),
              opacity_percent:Number(this.watermarkOpacity)
            } : null,
            marketplace_target: this.currentMarketplaceTarget ? {
              entity_kind:'marketplace_listing',
              listing_id:Number(this.currentMarketplaceTarget.listing_id),
              marketplace_code:this.currentMarketplaceTarget.marketplace_code,
              account_id:Number(this.currentMarketplaceTarget.account_id)
            } : null
          })
        });
        this.experiments = this.shuffle([...data.experiments, ...this.experiments]);
        this.showMessage(`Запущено вариантов: ${data.experiments.length}`, 'success');
      } catch (error) { this.showMessage(error.message, 'error'); }
      finally { this.submitting = false; }
    },
    async pollActive() {
      const active = this.experiments.filter(item => this.isActive(item)).slice(0, 10);
      await Promise.all(active.map(async item => {
        try {
          const data = await this.api(`/image-lab/api/experiments/${item.id}`);
          const index = this.experiments.findIndex(value => value.id === item.id);
          if (index >= 0) this.experiments.splice(index, 1, data.experiment);
        } catch (_) { /* transient poll failures stay retriable */ }
      }));
    },
    isActive(item) { return ['queued','running','remote_running','finalizing'].includes(item.status); },
    canCancel(item) { return item.status === 'queued' || item.status === 'remote_running'; },
    originalUrl(id, photoIndex = 0, preview = false) {
      const params = new URLSearchParams({photo_index: Number(photoIndex), v: this.sourceRevision});
      if (preview) params.set('preview', '1');
      return `/image-lab/api/products/${Number(id)}/original?${params.toString()}`;
    },
    artifactUrl(id, kind) { return `/image-lab/api/experiments/${Number(id)}/image/${kind}`; },
    statusLabel(item) {
      if (item.status === 'finalizing' && ['reference_guided','native_scene','angle_synthesis'].includes(item.generation_strategy)) return 'Проверка';
      return ({queued:'В очереди',running:'Генерация',remote_running:'GPU',finalizing:'Композит',completed:'Готово',failed:'Ошибка',cancelled:'Отменено'})[item.status] || item.status;
    },
    qualityLabel(item) { return ({auto_pass:'AUTO PASS',review_required:'REVIEW',rejected:'REJECTED'})[item.quality?.status] || '—'; },
    qualityClass(item) {
      return ({auto_pass:'bg-green-100 text-green-700',review_required:'bg-yellow-100 text-yellow-700',rejected:'bg-red-100 text-red-700'})[item.quality?.status] || 'bg-gray-100 text-gray-500';
    },
    sourceLabel(item) {
      const values = item.photo_indices || [0];
      let label = '';
      if (item.composition_mode === 'angles') label = `Ракурс: ${this.angleViewLabel(item.requested_view)} · ${values.length} реф.`;
      else if (item.composition_mode === 'collage') label = `Общий макет · ${values.length} фото`;
      else if (item.composition_mode === 'reference_set') label = `Герой: фото ${Number(item.primary_photo_index ?? values[0]) + 1} · ${values.length - 1} реф.`;
      else label = `Фото ${Number(values[0] || 0) + 1}`;
      if (item.marketplace_target) {
        label += ` · Ozon: ${item.marketplace_target.account_label || ('кабинет ' + item.marketplace_target.account_id)}`;
      }
      return label;
    },
    angleViewLabel(value) {
      return this.angleViews.find(item => item.id === value)?.label || value || '—';
    },
    strategyLabel(value) {
      return ({reference_guided:'masked edit · без второго слоя',background_only:'точный товар · локальный композит',native_scene:'нативная сцена · research',angle_synthesis:'новый ракурс · research'})[value] || value;
    },
    blindName(item, index) { return this.blindMode && !item.rating ? `Вариант ${String.fromCharCode(65 + (index % 26))}` : `${item.backend} · ${item.model}`; },
    draft(item) {
      if (!this.ratingDrafts[item.id]) this.ratingDrafts[item.id] = {
        rating: item.rating || 0, tags: [...(item.rating_tags || [])], comment: item.rating_comment || ''
      };
      return this.ratingDrafts[item.id];
    },
    toggleRatingTag(item, tag) {
      const draft = this.draft(item); const index = draft.tags.indexOf(tag);
      if (index >= 0) draft.tags.splice(index, 1); else if (draft.tags.length < 5) draft.tags.push(tag);
    },
    tagLabel(tag) {
      return ({good_composition:'Композиция',product_preserved:'Товар сохранён',angle_consistent:'Ракурс совпал',geometry_hallucination:'Геометрия выдумана',bad_background:'Плохой фон',bad_cutout:'Плохая маска',text_artifact:'Лишний текст',color_shift:'Сдвиг цвета',wrong_scale:'Неверный масштаб',other:'Другое'})[tag] || tag;
    },
    async saveRating(item) {
      try {
        const draft = this.draft(item);
        const data = await this.api(`/image-lab/api/experiments/${item.id}/rating`, {
          method:'POST', body:JSON.stringify(draft)
        });
        Object.assign(item, data.experiment); await this.refreshAnalytics();
        this.showMessage('Оценка сохранена', 'success');
      } catch (error) { this.showMessage(error.message, 'error'); }
    },
    async repeat(item) {
      try {
        const data = await this.api(`/image-lab/api/experiments/${item.id}/repeat`, {method:'POST', body:'{}'});
        this.experiments.unshift(data.experiment); this.showMessage('Повтор поставлен в очередь', 'success');
      } catch (error) { this.showMessage(error.message, 'error'); }
    },
    async cancel(item) {
      try {
        const data = await this.api(`/image-lab/api/experiments/${item.id}/cancel`, {
          method:'POST', body:'{}'
        });
        Object.assign(item, data.experiment); this.showMessage('Задача отменена', 'success');
      } catch (error) { this.showMessage(error.message, 'error'); }
    },
    async refreshAnalytics() {
      try { const data = await this.api('/image-lab/api/analytics'); this.analytics = data; } catch (_) {}
    },
    showMessage(text, type) { this.message = text; this.messageType = type; window.setTimeout(() => { if (this.message === text) this.message = ''; }, 6000); }
  };
}
