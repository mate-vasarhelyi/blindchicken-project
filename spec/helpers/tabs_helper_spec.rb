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

RSpec.describe TabsHelper do
  include described_class

  let(:given_tab) do
    { name: "avatar",
      partial: "avatars/users/avatar_tab",
      path: ->(params) { edit_user_path(params[:user], tab: :avatar) },
      label: :label_avatar }
  end

  let(:expected_tab) do
    { name: "avatar",
      partial: "avatars/users/avatar_tab",
      path: "/users/2/edit/avatar",
      label: :label_avatar }
  end

  describe "selected_tab" do
    let(:tabs) do
      [
        { name: "community", path: "/community", label: "Community" },
        { name: "enterprise", path: "/enterprise", label: "Enterprise", enterprise_feature: :custom_actions }
      ]
    end

    before do
      allow(EnterpriseToken).to receive(:hide_banners?).and_return(true)
      allow(EnterpriseToken).to receive(:allows_to?).and_call_original
    end

    it "selects the first available tab when the requested tab requires an unavailable Enterprise feature" do
      allow(params).to receive(:[]).with(:tab).and_return("enterprise")

      expect(selected_tab(tabs)[:name]).to eq("community")
    end
  end

  describe "tabs_for_key" do
    let(:current_user) { build(:user) }
    let(:user) { build(:user, id: 2) }

    before do
      allow(OpenProject::Ui::ExtensibleTabs)
        .to receive(:enabled_tabs)
        .with(:user, a_hash_including(user:, current_user:))
        .and_return [given_tab]
    end

    it "returns an evaluated path" do
      tabs = tabs_for_key(:user, user:)
      expect(response).to have_http_status :ok
      expect(tabs).to eq([expected_tab])
    end
  end
end
