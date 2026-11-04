# frozen_string_literal: true

#-- copyright
# OpenProject is an open source project management software.
# Copyright (C) the OpenProject GmbH
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License version 3.
#
# OpenProject is a fork of ChiliProject, which is a fork of Redmine. The copyright follows:
# Copyright (C) 2006-2013 Jean-Philippe Lang
# Copyright (C) 2010-2013 the ChiliProject Team
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
# See COPYRIGHT and LICENSE files for more details.
#++

require "spec_helper"

RSpec.describe "Admin design uploads",
               :skip_csrf,
               type: :rails_request,
               with_config: { ee_hide_banners: true },
               with_ee: false do
  let(:admin) { create(:admin) }
  let(:logo) { Rack::Test::UploadedFile.new(Rails.root.join("spec/support/custom_styles/logos/logo_image.png"), "image/png") }
  let(:favicon) do
    Rack::Test::UploadedFile.new(Rails.root.join("spec/support/custom_styles/favicons/favicon_image.png"), "image/png")
  end
  let(:touch_icon) do
    Rack::Test::UploadedFile.new(Rails.root.join("spec/support/custom_styles/touch_icons/touch_icon_image.png"), "image/png")
  end
  let(:export_logo) do
    Rack::Test::UploadedFile.new(Rails.root.join("spec/support/custom_styles/export_logos/export_logo_image.png"), "image/png")
  end
  let(:font) { Rack::Test::UploadedFile.new(Rails.public_path.join("fonts/noto/NotoSans-Regular.ttf"), "font/ttf") }
  let(:invalid_font) do
    Rack::Test::UploadedFile.new(Rails.root.join("spec/support/custom_styles/logos/logo_image.png"), "font/ttf")
  end

  before do
    login_as(admin)
  end

  def page
    Capybara.string(response.body)
  end

  describe "upload tabs" do
    it "renders one Save button for all branding images" do
      get "/admin/design", params: { tab: "branding" }

      expect(response).to have_http_status(:ok)
      expect(page).to have_css("[data-test-selector='custom-style-uploads-save']", count: 1)
      expect(page).to have_field("custom_style[logo]", type: :file)
      expect(page).to have_field("custom_style[logo_mobile]", type: :file)
      expect(page).to have_field("custom_style[favicon]", type: :file)
      expect(page).to have_field("custom_style[touch_icon]", type: :file)
      expect(page).to have_css("input[type='file'][required]", count: 0)
    end

    it "renders one Save button for each PDF upload tab" do
      %w[pdf_export_styles pdf_export_font].each do |tab|
        get "/admin/design", params: { tab: }

        expect(response).to have_http_status(:ok)
        expect(page).to have_css("[data-test-selector='custom-style-uploads-save']", count: 1)
      end
      expect(page).to have_css("input[type='file'][accept='.ttf,font/ttf']", count: 4)
    end
  end

  describe "saving uploads" do
    let!(:custom_style) { create(:custom_style) }

    it "saves all chosen branding images at once and keeps the favicon separate from the touch icon" do
      patch "/admin/design?tab=branding",
            params: { custom_style: { logo:, favicon:, touch_icon: } }

      expect(response).to redirect_to("/admin/design?tab=branding")
      custom_style.reload
      expect(custom_style.logo.file.filename).to eq("logo_image.png")
      expect(custom_style.favicon.file.filename).to eq("favicon_image.png")
      expect(custom_style.touch_icon.file.filename).to eq("touch_icon_image.png")
    end

    it "saves PDF export images and fonts" do
      patch "/admin/design?tab=pdf_export_styles", params: { custom_style: { export_logo: } }
      patch "/admin/design?tab=pdf_export_font", params: { custom_style: { export_font_regular: font } }

      custom_style.reload
      expect(custom_style.export_logo).to be_present
      expect(custom_style.export_font_regular).to be_present
    end

    it "returns to the tab without changes when no file is chosen" do
      patch "/admin/design?tab=branding"

      expect(response).to redirect_to("/admin/design?tab=branding")
      expect(custom_style.reload.logo).not_to be_present
    end

    it "shows the design page with the validation error for an invalid font" do
      patch "/admin/design?tab=pdf_export_font", params: { custom_style: { export_font_regular: invalid_font } }

      expect(response).to have_http_status(:unprocessable_entity)
      expect(page).to have_text(I18n.t("admin.custom_styles.fonts.file_is_invalid"))
      expect(page).to have_css("[data-test-selector='custom-style-uploads-save']", count: 1)
      expect(custom_style.reload.export_font_regular).not_to be_present
    end
  end

  describe "saving the color theme" do
    let!(:custom_style) { create(:custom_style, theme: nil) }

    it "explains that a theme must be selected when the current theme is custom" do
      post "/admin/design/themes?tab=branding"

      expect(response).to redirect_to("/admin/design?tab=branding")
      expect(flash[:error]).to eq("#{I18n.t('admin.custom_styles.color_theme')} " \
                                  "#{I18n.t('activerecord.errors.messages.blank')}")
      expect(custom_style.reload.theme).to be_nil
    end

    it "applies a selected theme" do
      theme = OpenProject::CustomStyles::ColorThemes.themes.first

      post "/admin/design/themes?tab=branding", params: { theme: theme[:theme] }

      expect(response).to redirect_to("/admin/design?tab=branding")
      expect(flash[:notice]).to eq(I18n.t(:notice_successful_update))
      expect(custom_style.reload.theme).to eq(theme[:theme])
    end
  end
end
