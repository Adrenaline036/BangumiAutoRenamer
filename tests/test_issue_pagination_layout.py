from nicegui import Client, ui

from nicegui.page import page

from src.pages.dashboard_view import TaskSection, DashboardView

from tests.test_source_tools_layout import SourceToolsLayout



class IssuePaginationLayout(SourceToolsLayout):

    def test_notice_matches_issue_card(self):

        container=self.render()

        cards=[e for e in container.descendants() if 'bar-issue-summary' in e._classes and any(getattr(child,'text','')=='当前没有纳入的文件' for child in e.descendants())]

        self.assertEqual(1,len(cards))

        card=cards[0]

        self.assertIs(container,card.parent_slot.parent.parent_slot.parent)

        self.assertIn('w-full',card._classes)
        self.assertTrue(card.visible)
        self.assertFalse(any('bar-error-text' in e._classes and getattr(e,'text','').startswith('当前没有纳入的文件') for e in container.descendants()))

        self.assertTrue(any('bar-error-title' in e._classes and e.text=='当前没有纳入的文件' for e in card.descendants()))

        self.assertTrue(any('bar-issue-detail' in e._classes and '内容保留与排除' in e.text for e in card.descendants()))

        self.assertTrue(any(e._props.get('name')=='o_warning' for e in card.descendants()))



    def test_all_sections_share_top_bottom_pagination(self):

        client=Client(page('/isolated-pages'),request=None)

        self.addCleanup(client.delete)

        with client:

            view=DashboardView(render_card=lambda row:ui.label(row['uuid']),refresh=lambda **kw:None)

        self.assertEqual({'active','review','history'},set(view.sections))

        for section in view.sections.values():

            root=section.container.parent_slot.parent

            buttons=[e for e in root.descendants() if e._props.get('label') in ('上一页','下一页')]

            self.assertEqual(4,len(buttons))

            ordered=list(root.descendants())
            split=ordered.index(section.container)
            self.assertTrue(all(ordered.index(b)<split for b in buttons[:2]))
            self.assertTrue(all(ordered.index(b)>split for b in buttons[2:]))
            previous=buttons[::2];following=buttons[1::2]
            def click(button):
                with client:
                    next(e for e in button._event_listeners.values() if e.type=='click').handler(None)

            for button in following:

                self.assertEqual('o_chevron_right',button._props.get('icon-right'))

                self.assertFalse(button._props.get('icon'))

            self.assertTrue(all(not b.enabled for b in buttons))

            rows=[{'uuid':str(i)} for i in range(41)]

            section.publish(rows)

            self.assertTrue(all(not b.enabled for b in previous));self.assertTrue(all(b.enabled for b in following))

            click(following[1])

            self.assertEqual(2,section.page);self.assertEqual({str(i) for i in range(20,40)},set(section.cards))

            self.assertTrue(all(b.enabled for b in buttons))

            click(following[0])

            self.assertEqual({'40'},set(section.cards));self.assertTrue(all(not b.enabled for b in following))

            click(previous[1]);self.assertEqual(2,section.page)
            click(previous[0]);self.assertEqual(1,section.page)
            click(following[1]);self.assertEqual(2,section.page)
            section.publish(rows[:1])

            self.assertEqual(1,section.page);self.assertEqual({'0'},set(section.cards))

            self.assertTrue(all(not b.enabled for b in buttons))

            section.publish([])

            self.assertTrue(section.empty.visible);self.assertFalse(section.cards)

