CREATE TABLE users (
	id INTEGER NOT NULL, 
	username VARCHAR(80) NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	is_admin BOOLEAN NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_login DATETIME, 
	blocked_at DATETIME, 
	blocked_by_user_id INTEGER, 
	notes TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(blocked_by_user_id) REFERENCES users (id)
);
CREATE UNIQUE INDEX ix_users_username ON users (username);
CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE TABLE category_mappings (
	id INTEGER NOT NULL, 
	source_category VARCHAR(200) NOT NULL, 
	source_type VARCHAR(50), 
	wb_category_name VARCHAR(200) NOT NULL, 
	wb_subject_id INTEGER NOT NULL, 
	wb_subject_name VARCHAR(200), 
	priority INTEGER, 
	is_auto_mapped BOOLEAN, 
	confidence_score FLOAT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_category_mapping UNIQUE (source_category, source_type, wb_subject_id)
);
CREATE INDEX ix_category_mappings_source_category ON category_mappings (source_category);
CREATE INDEX idx_category_source ON category_mappings (source_category, source_type);
CREATE TABLE marketplaces (
	id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	code VARCHAR(50) NOT NULL, 
	logo_url VARCHAR(500), 
	is_active BOOLEAN, 
	api_base_url VARCHAR(500), 
	api_key VARCHAR(500), 
	api_version VARCHAR(20), 
	categories_synced_at DATETIME, 
	categories_sync_status VARCHAR(50), 
	total_categories INTEGER, 
	total_characteristics INTEGER, 
	directories_synced_at DATETIME, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (code)
);
CREATE TABLE sellers (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	company_name VARCHAR(200) NOT NULL, 
	wb_api_key VARCHAR(500), 
	wb_seller_id VARCHAR(100), 
	contact_phone VARCHAR(20), 
	notes TEXT, 
	api_last_sync DATETIME, 
	api_sync_status VARCHAR(50), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE UNIQUE INDEX ix_sellers_user_id ON sellers (user_id);
CREATE TABLE user_activity (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	action VARCHAR(100) NOT NULL, 
	details TEXT, 
	ip_address VARCHAR(45), 
	user_agent VARCHAR(500), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_user_activity_created_at ON user_activity (created_at);
CREATE INDEX ix_user_activity_action ON user_activity (action);
CREATE INDEX idx_activity_user_created ON user_activity (user_id, created_at);
CREATE INDEX idx_activity_action_created ON user_activity (action, created_at);
CREATE INDEX ix_user_activity_user_id ON user_activity (user_id);
CREATE TABLE admin_audit_log (
	id INTEGER NOT NULL, 
	admin_user_id INTEGER NOT NULL, 
	action VARCHAR(100) NOT NULL, 
	target_type VARCHAR(50), 
	target_id INTEGER, 
	details TEXT, 
	ip_address VARCHAR(45), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(admin_user_id) REFERENCES users (id)
);
CREATE INDEX idx_audit_admin_created ON admin_audit_log (admin_user_id, created_at);
CREATE INDEX ix_admin_audit_log_created_at ON admin_audit_log (created_at);
CREATE INDEX ix_admin_audit_log_admin_user_id ON admin_audit_log (admin_user_id);
CREATE INDEX idx_audit_action_created ON admin_audit_log (action, created_at);
CREATE INDEX ix_admin_audit_log_action ON admin_audit_log (action);
CREATE INDEX idx_audit_target ON admin_audit_log (target_type, target_id);
CREATE TABLE system_settings (
	id INTEGER NOT NULL, 
	"key" VARCHAR(100) NOT NULL, 
	value TEXT, 
	value_type VARCHAR(20) NOT NULL, 
	description TEXT, 
	updated_by_user_id INTEGER, 
	updated_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
);
CREATE UNIQUE INDEX ix_system_settings_key ON system_settings ("key");
CREATE TABLE suppliers (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	code VARCHAR(50) NOT NULL, 
	description TEXT, 
	website VARCHAR(500), 
	logo_url VARCHAR(500), 
	csv_source_url VARCHAR(500), 
	csv_delimiter VARCHAR(5), 
	csv_encoding VARCHAR(20), 
	api_endpoint VARCHAR(500), 
	price_file_url VARCHAR(500), 
	price_file_inf_url VARCHAR(500), 
	price_file_delimiter VARCHAR(5), 
	price_file_encoding VARCHAR(20), 
	last_price_sync_at DATETIME, 
	last_price_sync_status VARCHAR(50), 
	last_price_sync_error TEXT, 
	last_price_file_hash VARCHAR(64), 
	auto_sync_prices BOOLEAN NOT NULL, 
	auto_sync_interval_minutes INTEGER, 
	auth_login VARCHAR(200), 
	auth_password VARCHAR(500), 
	ai_enabled BOOLEAN NOT NULL, 
	ai_provider VARCHAR(50), 
	ai_api_key VARCHAR(500), 
	ai_api_base_url VARCHAR(500), 
	ai_model VARCHAR(100), 
	ai_temperature FLOAT, 
	ai_max_tokens INTEGER, 
	ai_timeout INTEGER, 
	ai_client_id VARCHAR(500), 
	ai_client_secret VARCHAR(500), 
	ai_category_instruction TEXT, 
	ai_size_instruction TEXT, 
	ai_seo_title_instruction TEXT, 
	ai_keywords_instruction TEXT, 
	ai_description_instruction TEXT, 
	ai_analysis_instruction TEXT, 
	ai_parsing_instruction TEXT, 
	description_file_url VARCHAR(500), 
	description_file_delimiter VARCHAR(5), 
	description_file_encoding VARCHAR(20), 
	last_description_sync_at DATETIME, 
	last_description_sync_status VARCHAR(50), 
	resize_images BOOLEAN NOT NULL, 
	image_target_size INTEGER, 
	image_background_color VARCHAR(20), 
	default_markup_percent FLOAT, 
	is_active BOOLEAN NOT NULL, 
	total_products INTEGER, 
	last_sync_at DATETIME, 
	last_sync_status VARCHAR(50), 
	last_sync_error TEXT, 
	last_sync_duration FLOAT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	created_by_user_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id)
);
CREATE UNIQUE INDEX ix_suppliers_code ON suppliers (code);
CREATE TABLE marketplace_categories (
	id INTEGER NOT NULL, 
	marketplace_id INTEGER NOT NULL, 
	subject_id INTEGER NOT NULL, 
	subject_name VARCHAR(300), 
	parent_id INTEGER, 
	parent_name VARCHAR(300), 
	is_enabled BOOLEAN, 
	is_leaf BOOLEAN, 
	characteristics_synced_at DATETIME, 
	characteristics_count INTEGER, 
	required_count INTEGER, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mp_category UNIQUE (marketplace_id, subject_id), 
	FOREIGN KEY(marketplace_id) REFERENCES marketplaces (id)
);
CREATE INDEX idx_mp_category_parent ON marketplace_categories (marketplace_id, parent_id);
CREATE INDEX idx_mp_category_enabled ON marketplace_categories (marketplace_id, is_enabled);
CREATE TABLE marketplace_directories (
	id INTEGER NOT NULL, 
	marketplace_id INTEGER NOT NULL, 
	directory_type VARCHAR(50) NOT NULL, 
	data_json TEXT NOT NULL, 
	synced_at DATETIME, 
	items_count INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mp_directory UNIQUE (marketplace_id, directory_type), 
	FOREIGN KEY(marketplace_id) REFERENCES marketplaces (id)
);
CREATE TABLE marketplace_sync_jobs (
	id VARCHAR(36) NOT NULL, 
	marketplace_id INTEGER NOT NULL, 
	job_type VARCHAR(30), 
	status VARCHAR(20), 
	total INTEGER, 
	processed INTEGER, 
	error_message TEXT, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(marketplace_id) REFERENCES marketplaces (id)
);
CREATE TABLE seller_reports (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	statistics_path VARCHAR(500) NOT NULL, 
	price_path VARCHAR(500) NOT NULL, 
	processed_path VARCHAR(500) NOT NULL, 
	selected_columns JSON NOT NULL, 
	summary JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE INDEX ix_seller_reports_seller_id ON seller_reports (seller_id);
CREATE TABLE products (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	nm_id BIGINT NOT NULL, 
	imt_id BIGINT, 
	vendor_code VARCHAR(100), 
	title VARCHAR(500), 
	brand VARCHAR(200), 
	object_name VARCHAR(200), 
	subject_id INTEGER, 
	supplier_vendor_code VARCHAR(100), 
	price NUMERIC(10, 2), 
	discount_price NUMERIC(10, 2), 
	quantity INTEGER, 
	supplier_price FLOAT, 
	supplier_price_updated_at DATETIME, 
	photos_json TEXT, 
	video_url VARCHAR(500), 
	sizes_json TEXT, 
	characteristics_json TEXT, 
	description TEXT, 
	dimensions_json TEXT, 
	tags_json TEXT, 
	is_active BOOLEAN, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	last_sync DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE INDEX ix_products_seller_id ON products (seller_id);
CREATE INDEX ix_products_nm_id ON products (nm_id);
CREATE INDEX ix_products_vendor_code ON products (vendor_code);
CREATE INDEX idx_seller_nm_id ON products (seller_id, nm_id);
CREATE INDEX idx_seller_active ON products (seller_id, is_active);
CREATE INDEX idx_seller_vendor_code ON products (seller_id, vendor_code);
CREATE TABLE api_logs (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	endpoint VARCHAR(200) NOT NULL, 
	method VARCHAR(10) NOT NULL, 
	status_code INTEGER, 
	response_time FLOAT, 
	request_body TEXT, 
	response_body TEXT, 
	success BOOLEAN, 
	error_message TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE INDEX ix_api_logs_seller_id ON api_logs (seller_id);
CREATE INDEX ix_api_logs_created_at ON api_logs (created_at);
CREATE INDEX idx_seller_created ON api_logs (seller_id, created_at);
CREATE TABLE bulk_edit_history (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	operation_type VARCHAR(50) NOT NULL, 
	operation_params JSON, 
	description TEXT, 
	total_products INTEGER, 
	success_count INTEGER, 
	error_count INTEGER, 
	errors_details JSON, 
	status VARCHAR(50), 
	wb_synced BOOLEAN, 
	reverted BOOLEAN, 
	reverted_at DATETIME, 
	reverted_by_user_id INTEGER, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	duration_seconds FLOAT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id), 
	FOREIGN KEY(reverted_by_user_id) REFERENCES users (id)
);
CREATE INDEX ix_bulk_edit_history_seller_id ON bulk_edit_history (seller_id);
CREATE INDEX ix_bulk_edit_history_created_at ON bulk_edit_history (created_at);
CREATE TABLE price_monitor_settings (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	is_enabled BOOLEAN NOT NULL, 
	monitor_prices BOOLEAN NOT NULL, 
	monitor_stocks BOOLEAN NOT NULL, 
	sync_interval_minutes INTEGER NOT NULL, 
	price_change_threshold_percent FLOAT NOT NULL, 
	stock_change_threshold_percent FLOAT NOT NULL, 
	last_sync_at DATETIME, 
	last_sync_status VARCHAR(50), 
	last_sync_error TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE UNIQUE INDEX ix_price_monitor_settings_seller_id ON price_monitor_settings (seller_id);
CREATE TABLE product_sync_settings (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	is_enabled BOOLEAN NOT NULL, 
	sync_interval_minutes INTEGER NOT NULL, 
	sync_products BOOLEAN NOT NULL, 
	sync_stocks BOOLEAN NOT NULL, 
	last_sync_at DATETIME, 
	next_sync_at DATETIME, 
	last_sync_status VARCHAR(50), 
	last_sync_error TEXT, 
	last_sync_duration FLOAT, 
	products_synced INTEGER, 
	products_added INTEGER, 
	products_updated INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE UNIQUE INDEX ix_product_sync_settings_seller_id ON product_sync_settings (seller_id);
CREATE TABLE auto_import_settings (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	is_enabled BOOLEAN NOT NULL, 
	supplier_code VARCHAR(50), 
	vendor_code_pattern VARCHAR(200), 
	csv_source_url VARCHAR(500), 
	csv_source_type VARCHAR(50), 
	csv_delimiter VARCHAR(5), 
	sexoptovik_login VARCHAR(200), 
	sexoptovik_password VARCHAR(200), 
	import_only_new BOOLEAN NOT NULL, 
	auto_enable_products BOOLEAN NOT NULL, 
	use_blurred_images BOOLEAN NOT NULL, 
	resize_images_to_1200 BOOLEAN NOT NULL, 
	image_background_color VARCHAR(20), 
	ai_enabled BOOLEAN NOT NULL, 
	ai_provider VARCHAR(50), 
	ai_api_key VARCHAR(500), 
	ai_api_base_url VARCHAR(500), 
	ai_model VARCHAR(100), 
	ai_temperature FLOAT, 
	ai_max_tokens INTEGER, 
	ai_timeout INTEGER, 
	ai_use_for_categories BOOLEAN NOT NULL, 
	ai_use_for_sizes BOOLEAN NOT NULL, 
	ai_category_confidence_threshold FLOAT, 
	ai_top_p FLOAT, 
	ai_presence_penalty FLOAT, 
	ai_frequency_penalty FLOAT, 
	ai_category_instruction TEXT, 
	ai_size_instruction TEXT, 
	ai_seo_title_instruction TEXT, 
	ai_keywords_instruction TEXT, 
	ai_bullets_instruction TEXT, 
	ai_description_instruction TEXT, 
	ai_rich_content_instruction TEXT, 
	ai_analysis_instruction TEXT, 
	ai_dimensions_instruction TEXT, 
	ai_clothing_sizes_instruction TEXT, 
	ai_brand_instruction TEXT, 
	ai_material_instruction TEXT, 
	ai_color_instruction TEXT, 
	ai_attributes_instruction TEXT, 
	ai_client_id VARCHAR(500), 
	ai_client_secret VARCHAR(500), 
	image_gen_enabled BOOLEAN NOT NULL, 
	image_gen_provider VARCHAR(50), 
	fluxapi_key VARCHAR(500), 
	tensorart_app_id VARCHAR(500), 
	tensorart_api_key VARCHAR(500), 
	together_api_key VARCHAR(500), 
	openai_api_key VARCHAR(500), 
	replicate_api_key VARCHAR(500), 
	image_gen_width INTEGER, 
	image_gen_height INTEGER, 
	openai_image_quality VARCHAR(20), 
	openai_image_style VARCHAR(20), 
	auto_import_interval_hours INTEGER NOT NULL, 
	last_import_at DATETIME, 
	next_import_at DATETIME, 
	last_import_status VARCHAR(50), 
	last_import_error TEXT, 
	last_import_duration FLOAT, 
	total_products_found INTEGER, 
	products_imported INTEGER, 
	products_skipped INTEGER, 
	products_failed INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE UNIQUE INDEX ix_auto_import_settings_seller_id ON auto_import_settings (seller_id);
CREATE TABLE pricing_settings (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	is_enabled BOOLEAN NOT NULL, 
	formula_type VARCHAR(50), 
	supplier_price_url VARCHAR(500), 
	supplier_price_inf_url VARCHAR(500), 
	last_price_sync_at DATETIME, 
	last_price_file_hash VARCHAR(64), 
	wb_commission_pct FLOAT NOT NULL, 
	tax_rate FLOAT NOT NULL, 
	logistics_cost FLOAT NOT NULL, 
	storage_cost FLOAT NOT NULL, 
	packaging_cost FLOAT NOT NULL, 
	acquiring_cost FLOAT NOT NULL, 
	extra_cost FLOAT NOT NULL, 
	delivery_pct FLOAT NOT NULL, 
	delivery_min FLOAT NOT NULL, 
	delivery_max FLOAT NOT NULL, 
	profit_column VARCHAR(1) NOT NULL, 
	min_profit FLOAT NOT NULL, 
	max_profit FLOAT, 
	use_random BOOLEAN NOT NULL, 
	random_min INTEGER NOT NULL, 
	random_max INTEGER NOT NULL, 
	spp_pct FLOAT NOT NULL, 
	spp_min FLOAT NOT NULL, 
	spp_max FLOAT NOT NULL, 
	inflated_multiplier FLOAT NOT NULL, 
	price_ranges TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE UNIQUE INDEX ix_pricing_settings_seller_id ON pricing_settings (seller_id);
CREATE TABLE card_merge_history (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	operation_type VARCHAR(20) NOT NULL, 
	target_imt_id BIGINT, 
	merged_nm_ids JSON NOT NULL, 
	snapshot_before JSON, 
	snapshot_after JSON, 
	status VARCHAR(50), 
	wb_synced BOOLEAN, 
	wb_sync_status VARCHAR(50), 
	wb_error_message TEXT, 
	reverted BOOLEAN, 
	reverted_at DATETIME, 
	reverted_by_user_id INTEGER, 
	revert_operation_id INTEGER, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	duration_seconds FLOAT, 
	user_comment TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id), 
	FOREIGN KEY(reverted_by_user_id) REFERENCES users (id), 
	FOREIGN KEY(revert_operation_id) REFERENCES card_merge_history (id)
);
CREATE INDEX ix_card_merge_history_seller_id ON card_merge_history (seller_id);
CREATE INDEX idx_merge_seller_created ON card_merge_history (seller_id, created_at);
CREATE INDEX ix_card_merge_history_created_at ON card_merge_history (created_at);
CREATE INDEX ix_card_merge_history_target_imt_id ON card_merge_history (target_imt_id);
CREATE INDEX idx_merge_operation ON card_merge_history (operation_type, status);
CREATE TABLE safe_price_change_settings (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	is_enabled BOOLEAN NOT NULL, 
	safe_threshold_percent FLOAT NOT NULL, 
	warning_threshold_percent FLOAT NOT NULL, 
	mode VARCHAR(20) NOT NULL, 
	require_comment_for_dangerous BOOLEAN NOT NULL, 
	allow_bulk_dangerous BOOLEAN NOT NULL, 
	max_products_per_batch INTEGER NOT NULL, 
	allow_unlimited_batch BOOLEAN NOT NULL, 
	notify_on_dangerous BOOLEAN NOT NULL, 
	notify_email VARCHAR(200), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE UNIQUE INDEX ix_safe_price_change_settings_seller_id ON safe_price_change_settings (seller_id);
CREATE TABLE price_change_batches (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	name VARCHAR(200), 
	description TEXT, 
	change_type VARCHAR(50) NOT NULL, 
	change_value FLOAT, 
	change_formula VARCHAR(500), 
	status VARCHAR(30) NOT NULL, 
	has_safe_changes BOOLEAN, 
	has_warning_changes BOOLEAN, 
	has_dangerous_changes BOOLEAN, 
	total_items INTEGER, 
	safe_count INTEGER, 
	warning_count INTEGER, 
	dangerous_count INTEGER, 
	applied_count INTEGER, 
	failed_count INTEGER, 
	confirmed_at DATETIME, 
	confirmed_by_user_id INTEGER, 
	confirmation_comment TEXT, 
	applied_at DATETIME, 
	wb_task_id VARCHAR(100), 
	apply_errors JSON, 
	reverted BOOLEAN, 
	reverted_at DATETIME, 
	reverted_by_user_id INTEGER, 
	revert_batch_id INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id), 
	FOREIGN KEY(confirmed_by_user_id) REFERENCES users (id), 
	FOREIGN KEY(reverted_by_user_id) REFERENCES users (id), 
	FOREIGN KEY(revert_batch_id) REFERENCES price_change_batches (id)
);
CREATE INDEX ix_price_change_batches_status ON price_change_batches (status);
CREATE INDEX idx_price_batch_seller_status ON price_change_batches (seller_id, status);
CREATE INDEX ix_price_change_batches_seller_id ON price_change_batches (seller_id);
CREATE INDEX idx_price_batch_seller_created ON price_change_batches (seller_id, created_at);
CREATE INDEX ix_price_change_batches_created_at ON price_change_batches (created_at);
CREATE TABLE blocked_cards (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	nm_id BIGINT NOT NULL, 
	vendor_code VARCHAR(200), 
	title VARCHAR(500), 
	brand VARCHAR(200), 
	reason TEXT, 
	first_seen_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE INDEX ix_blocked_cards_seller_id ON blocked_cards (seller_id);
CREATE INDEX idx_blocked_seller_nm ON blocked_cards (seller_id, nm_id);
CREATE INDEX idx_blocked_seller_active ON blocked_cards (seller_id, is_active);
CREATE TABLE shadowed_cards (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	nm_id BIGINT NOT NULL, 
	vendor_code VARCHAR(200), 
	title VARCHAR(500), 
	brand VARCHAR(200), 
	nm_rating FLOAT, 
	first_seen_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE INDEX idx_shadowed_seller_nm ON shadowed_cards (seller_id, nm_id);
CREATE INDEX idx_shadowed_seller_active ON shadowed_cards (seller_id, is_active);
CREATE INDEX ix_shadowed_cards_seller_id ON shadowed_cards (seller_id);
CREATE TABLE blocked_cards_sync_settings (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	last_sync_at DATETIME, 
	last_sync_status VARCHAR(20), 
	last_sync_error TEXT, 
	blocked_count INTEGER, 
	shadowed_count INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (seller_id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE TABLE supplier_products (
	id INTEGER NOT NULL, 
	supplier_id INTEGER NOT NULL, 
	external_id VARCHAR(200), 
	vendor_code VARCHAR(200), 
	barcode VARCHAR(200), 
	title VARCHAR(500), 
	description TEXT, 
	brand VARCHAR(200), 
	category VARCHAR(200), 
	all_categories TEXT, 
	wb_category_name VARCHAR(200), 
	wb_subject_id INTEGER, 
	wb_subject_name VARCHAR(200), 
	category_confidence FLOAT, 
	supplier_price FLOAT, 
	supplier_quantity INTEGER, 
	currency VARCHAR(10), 
	recommended_retail_price FLOAT, 
	supplier_status VARCHAR(50), 
	additional_vendor_code VARCHAR(200), 
	last_price_sync_at DATETIME, 
	price_changed_at DATETIME, 
	previous_price FLOAT, 
	characteristics_json TEXT, 
	sizes_json TEXT, 
	colors_json TEXT, 
	materials_json TEXT, 
	dimensions_json TEXT, 
	gender VARCHAR(50), 
	country VARCHAR(100), 
	season VARCHAR(50), 
	age_group VARCHAR(50), 
	photo_urls_json TEXT, 
	processed_photos_json TEXT, 
	video_url VARCHAR(500), 
	ai_seo_title VARCHAR(500), 
	ai_description TEXT, 
	ai_keywords_json TEXT, 
	ai_bullets_json TEXT, 
	ai_rich_content_json TEXT, 
	ai_analysis_json TEXT, 
	ai_validated BOOLEAN, 
	ai_validated_at DATETIME, 
	ai_validation_score FLOAT, 
	content_hash VARCHAR(64), 
	ai_parsed_data_json TEXT, 
	ai_parsed_at DATETIME, 
	ai_marketplace_json TEXT, 
	description_source VARCHAR(50), 
	original_data_json TEXT, 
	status VARCHAR(50), 
	validation_errors_json TEXT, 
	marketplace_fields_json TEXT, 
	marketplace_validation_status VARCHAR(50), 
	marketplace_fill_pct FLOAT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_supplier_external_id UNIQUE (supplier_id, external_id), 
	FOREIGN KEY(supplier_id) REFERENCES suppliers (id)
);
CREATE INDEX idx_supplier_product_category ON supplier_products (supplier_id, wb_subject_id);
CREATE INDEX idx_supplier_product_status ON supplier_products (supplier_id, status);
CREATE INDEX idx_supplier_product_brand ON supplier_products (supplier_id, brand);
CREATE INDEX ix_supplier_products_supplier_id ON supplier_products (supplier_id);
CREATE INDEX ix_supplier_products_status ON supplier_products (status);
CREATE INDEX ix_supplier_products_external_id ON supplier_products (external_id);
CREATE TABLE seller_suppliers (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	supplier_id INTEGER NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	supplier_code VARCHAR(50), 
	vendor_code_pattern VARCHAR(200), 
	custom_markup_percent FLOAT, 
	products_imported INTEGER, 
	last_import_at DATETIME, 
	connected_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_seller_supplier UNIQUE (seller_id, supplier_id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id), 
	FOREIGN KEY(supplier_id) REFERENCES suppliers (id)
);
CREATE INDEX idx_seller_supplier_active ON seller_suppliers (seller_id, is_active);
CREATE INDEX ix_seller_suppliers_seller_id ON seller_suppliers (seller_id);
CREATE INDEX ix_seller_suppliers_supplier_id ON seller_suppliers (supplier_id);
CREATE TABLE enrichment_jobs (
	id VARCHAR(36) NOT NULL, 
	seller_id INTEGER NOT NULL, 
	status VARCHAR(20), 
	total INTEGER, 
	processed INTEGER, 
	succeeded INTEGER, 
	failed INTEGER, 
	skipped INTEGER, 
	fields_config TEXT, 
	photo_strategy VARCHAR(20), 
	results TEXT, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE TABLE ai_parse_jobs (
	id VARCHAR(36) NOT NULL, 
	supplier_id INTEGER NOT NULL, 
	admin_user_id INTEGER, 
	job_type VARCHAR(30), 
	status VARCHAR(20), 
	total INTEGER, 
	processed INTEGER, 
	succeeded INTEGER, 
	failed INTEGER, 
	current_product_title VARCHAR(200), 
	results TEXT, 
	error_message TEXT, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(supplier_id) REFERENCES suppliers (id)
);
CREATE TABLE marketplace_category_characteristics (
	id INTEGER NOT NULL, 
	category_id INTEGER NOT NULL, 
	marketplace_id INTEGER NOT NULL, 
	charc_id INTEGER NOT NULL, 
	name VARCHAR(300) NOT NULL, 
	charc_type INTEGER NOT NULL, 
	required BOOLEAN, 
	unit_name VARCHAR(50), 
	max_count INTEGER, 
	popular BOOLEAN, 
	dictionary_json TEXT, 
	ai_instruction TEXT, 
	ai_example_value VARCHAR(500), 
	is_enabled BOOLEAN, 
	display_order INTEGER, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_category_charc UNIQUE (category_id, charc_id), 
	FOREIGN KEY(category_id) REFERENCES marketplace_categories (id), 
	FOREIGN KEY(marketplace_id) REFERENCES marketplaces (id)
);
CREATE INDEX idx_mp_charc_required ON marketplace_category_characteristics (category_id, required);
CREATE TABLE marketplace_connections (
	id INTEGER NOT NULL, 
	supplier_id INTEGER NOT NULL, 
	marketplace_id INTEGER NOT NULL, 
	is_active BOOLEAN, 
	enabled_categories_json TEXT, 
	auto_map_categories BOOLEAN, 
	default_category_id INTEGER, 
	products_mapped INTEGER, 
	products_validated INTEGER, 
	last_mapping_at DATETIME, 
	connected_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_supplier_marketplace UNIQUE (supplier_id, marketplace_id), 
	FOREIGN KEY(supplier_id) REFERENCES suppliers (id), 
	FOREIGN KEY(marketplace_id) REFERENCES marketplaces (id)
);
CREATE TABLE card_edit_history (
	id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	bulk_edit_id INTEGER, 
	action VARCHAR(50) NOT NULL, 
	changed_fields JSON, 
	snapshot_before JSON, 
	snapshot_after JSON, 
	wb_synced BOOLEAN, 
	wb_sync_status VARCHAR(50), 
	wb_error_message TEXT, 
	reverted BOOLEAN, 
	reverted_at DATETIME, 
	reverted_by_history_id INTEGER, 
	created_at DATETIME NOT NULL, 
	user_comment TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(product_id) REFERENCES products (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id), 
	FOREIGN KEY(bulk_edit_id) REFERENCES bulk_edit_history (id), 
	FOREIGN KEY(reverted_by_history_id) REFERENCES card_edit_history (id)
);
CREATE INDEX ix_card_edit_history_seller_id ON card_edit_history (seller_id);
CREATE INDEX ix_card_edit_history_bulk_edit_id ON card_edit_history (bulk_edit_id);
CREATE INDEX ix_card_edit_history_created_at ON card_edit_history (created_at);
CREATE INDEX ix_card_edit_history_product_id ON card_edit_history (product_id);
CREATE TABLE product_stocks (
	id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	warehouse_id INTEGER, 
	warehouse_name VARCHAR(200), 
	quantity INTEGER, 
	quantity_full INTEGER, 
	in_way_to_client INTEGER, 
	in_way_from_client INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_product_warehouse UNIQUE (product_id, warehouse_id), 
	FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE CASCADE
);
CREATE INDEX idx_product_stocks_warehouse_id ON product_stocks (warehouse_id);
CREATE INDEX idx_product_stocks_product_id ON product_stocks (product_id);
CREATE INDEX ix_product_stocks_warehouse_id ON product_stocks (warehouse_id);
CREATE INDEX ix_product_stocks_product_id ON product_stocks (product_id);
CREATE TABLE imported_products (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	product_id INTEGER, 
	supplier_product_id INTEGER, 
	supplier_id INTEGER, 
	external_id VARCHAR(200), 
	external_vendor_code VARCHAR(200), 
	source_type VARCHAR(50), 
	title VARCHAR(500), 
	category VARCHAR(200), 
	all_categories TEXT, 
	mapped_wb_category VARCHAR(200), 
	wb_subject_id INTEGER, 
	category_confidence FLOAT, 
	brand VARCHAR(200), 
	country VARCHAR(100), 
	gender VARCHAR(50), 
	colors TEXT, 
	sizes TEXT, 
	materials TEXT, 
	photo_urls TEXT, 
	processed_photos TEXT, 
	barcodes TEXT, 
	characteristics TEXT, 
	description TEXT, 
	original_data TEXT, 
	supplier_price FLOAT, 
	supplier_quantity INTEGER, 
	calculated_price FLOAT, 
	calculated_discount_price FLOAT, 
	calculated_price_before_discount FLOAT, 
	import_status VARCHAR(50), 
	validation_errors TEXT, 
	import_error TEXT, 
	ai_keywords TEXT, 
	ai_bullets TEXT, 
	ai_rich_content TEXT, 
	ai_seo_title VARCHAR(500), 
	ai_analysis TEXT, 
	ai_analysis_at DATETIME, 
	content_hash VARCHAR(64), 
	ai_dimensions TEXT, 
	ai_clothing_sizes TEXT, 
	ai_detected_brand TEXT, 
	ai_materials TEXT, 
	ai_colors TEXT, 
	ai_attributes TEXT, 
	ai_gender VARCHAR(20), 
	ai_age_group VARCHAR(20), 
	ai_season VARCHAR(20), 
	ai_country VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	imported_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id), 
	FOREIGN KEY(product_id) REFERENCES products (id), 
	FOREIGN KEY(supplier_product_id) REFERENCES supplier_products (id), 
	FOREIGN KEY(supplier_id) REFERENCES suppliers (id)
);
CREATE INDEX ix_imported_products_seller_id ON imported_products (seller_id);
CREATE INDEX ix_imported_products_external_id ON imported_products (external_id);
CREATE INDEX idx_imported_seller_status ON imported_products (seller_id, import_status);
CREATE INDEX ix_imported_products_supplier_id ON imported_products (supplier_id);
CREATE INDEX ix_imported_products_product_id ON imported_products (product_id);
CREATE INDEX idx_imported_external_id ON imported_products (external_id, source_type);
CREATE INDEX ix_imported_products_supplier_product_id ON imported_products (supplier_product_id);
CREATE INDEX idx_imported_supplier_product ON imported_products (supplier_product_id);
CREATE TABLE price_history (
	id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	old_price NUMERIC(10, 2), 
	old_discount_price NUMERIC(10, 2), 
	old_quantity INTEGER, 
	new_price NUMERIC(10, 2), 
	new_discount_price NUMERIC(10, 2), 
	new_quantity INTEGER, 
	price_change_percent FLOAT, 
	discount_price_change_percent FLOAT, 
	quantity_change_percent FLOAT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE CASCADE, 
	FOREIGN KEY(seller_id) REFERENCES sellers (id)
);
CREATE INDEX ix_price_history_product_id ON price_history (product_id);
CREATE INDEX idx_price_history_product_created ON price_history (product_id, created_at);
CREATE INDEX ix_price_history_seller_id ON price_history (seller_id);
CREATE INDEX idx_price_history_seller_created ON price_history (seller_id, created_at);
CREATE INDEX ix_price_history_created_at ON price_history (created_at);
CREATE TABLE price_change_items (
	id INTEGER NOT NULL, 
	batch_id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	nm_id BIGINT NOT NULL, 
	vendor_code VARCHAR(100), 
	product_title VARCHAR(500), 
	old_price NUMERIC(10, 2), 
	old_discount INTEGER, 
	old_discount_price NUMERIC(10, 2), 
	new_price NUMERIC(10, 2), 
	new_discount INTEGER, 
	new_discount_price NUMERIC(10, 2), 
	price_change_amount NUMERIC(10, 2), 
	price_change_percent FLOAT, 
	safety_level VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	error_message TEXT, 
	wb_applied_at DATETIME, 
	wb_status VARCHAR(50), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(batch_id) REFERENCES price_change_batches (id) ON DELETE CASCADE, 
	FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE CASCADE
);
CREATE INDEX idx_price_item_batch_safety ON price_change_items (batch_id, safety_level);
CREATE INDEX ix_price_change_items_safety_level ON price_change_items (safety_level);
CREATE INDEX ix_price_change_items_product_id ON price_change_items (product_id);
CREATE INDEX idx_price_item_batch_status ON price_change_items (batch_id, status);
CREATE INDEX ix_price_change_items_nm_id ON price_change_items (nm_id);
CREATE INDEX ix_price_change_items_batch_id ON price_change_items (batch_id);
CREATE TABLE product_category_corrections (
	id INTEGER NOT NULL, 
	imported_product_id INTEGER, 
	external_id VARCHAR(200), 
	source_type VARCHAR(50), 
	product_title VARCHAR(500), 
	original_category VARCHAR(200), 
	corrected_wb_subject_id INTEGER NOT NULL, 
	corrected_wb_subject_name VARCHAR(200), 
	corrected_by_user_id INTEGER, 
	correction_reason TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(imported_product_id) REFERENCES imported_products (id), 
	FOREIGN KEY(corrected_by_user_id) REFERENCES users (id)
);
CREATE INDEX idx_correction_category ON product_category_corrections (original_category, source_type);
CREATE INDEX ix_product_category_corrections_imported_product_id ON product_category_corrections (imported_product_id);
CREATE INDEX idx_correction_external ON product_category_corrections (external_id, source_type);
CREATE INDEX ix_product_category_corrections_external_id ON product_category_corrections (external_id);
CREATE TABLE suspicious_price_changes (
	id INTEGER NOT NULL, 
	price_history_id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	change_type VARCHAR(50) NOT NULL, 
	old_value NUMERIC(10, 2), 
	new_value NUMERIC(10, 2), 
	change_percent FLOAT NOT NULL, 
	threshold_percent FLOAT NOT NULL, 
	is_reviewed BOOLEAN NOT NULL, 
	reviewed_at DATETIME, 
	reviewed_by_user_id INTEGER, 
	notes TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(price_history_id) REFERENCES price_history (id) ON DELETE CASCADE, 
	FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE CASCADE, 
	FOREIGN KEY(seller_id) REFERENCES sellers (id), 
	FOREIGN KEY(reviewed_by_user_id) REFERENCES users (id)
);
CREATE INDEX ix_suspicious_price_changes_change_type ON suspicious_price_changes (change_type);
CREATE INDEX ix_suspicious_price_changes_seller_id ON suspicious_price_changes (seller_id);
CREATE INDEX idx_suspicious_product_created ON suspicious_price_changes (product_id, created_at);
CREATE INDEX idx_suspicious_seller_created ON suspicious_price_changes (seller_id, created_at);
CREATE INDEX ix_suspicious_price_changes_price_history_id ON suspicious_price_changes (price_history_id);
CREATE INDEX ix_suspicious_price_changes_created_at ON suspicious_price_changes (created_at);
CREATE INDEX ix_suspicious_price_changes_is_reviewed ON suspicious_price_changes (is_reviewed);
CREATE INDEX idx_suspicious_seller_reviewed ON suspicious_price_changes (seller_id, is_reviewed);
CREATE INDEX ix_suspicious_price_changes_product_id ON suspicious_price_changes (product_id);
CREATE TABLE ai_history (
	id INTEGER NOT NULL, 
	seller_id INTEGER NOT NULL, 
	imported_product_id INTEGER, 
	action_type VARCHAR(50) NOT NULL, 
	ai_provider VARCHAR(50), 
	ai_model VARCHAR(100), 
	system_prompt TEXT, 
	user_prompt TEXT, 
	input_data TEXT, 
	result_data TEXT, 
	raw_response TEXT, 
	success BOOLEAN, 
	error_message TEXT, 
	tokens_used INTEGER, 
	tokens_prompt INTEGER, 
	tokens_completion INTEGER, 
	response_time_ms INTEGER, 
	source_module VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(seller_id) REFERENCES sellers (id), 
	FOREIGN KEY(imported_product_id) REFERENCES imported_products (id)
);
CREATE INDEX idx_ai_history_seller_action ON ai_history (seller_id, action_type);
CREATE INDEX ix_ai_history_action_type ON ai_history (action_type);
CREATE INDEX ix_ai_history_seller_id ON ai_history (seller_id);
CREATE INDEX idx_ai_history_product_created ON ai_history (imported_product_id, created_at);
CREATE INDEX ix_ai_history_imported_product_id ON ai_history (imported_product_id);
CREATE INDEX idx_ai_history_created ON ai_history (created_at);
CREATE INDEX ix_ai_history_created_at ON ai_history (created_at);
