import re
import unicodedata

import gi
gi.require_version('Gtk', '3.0')

from gi.repository import Gtk as gtk

from zim.plugins import PluginClass
from zim.signals import SIGNAL_AFTER, ConnectorMixin
from zim.actions import toggle_action

from zim.gui.pageview import PageViewExtension
from zim.gui.widgets import ErrorDialog

try:
	import enchant
	enchant_found = True
except ImportError:
	enchant_found = False

def _strip_diacritics(text):
	return ''.join(c
				   for c in unicodedata.normalize('NFKD', text)
				   if unicodedata.category(c) != 'Mn' and not unicodedata.combining(c))

def _remove_punctuation_and_spaces(text):
	return ''.join(c
				   for c in text
				   if unicodedata.category(c)[0] not in ['P', 'Z'])

def _expand_ligatures(text):
	"""
	Expands the ligatures 'œ' and 'æ' to their two-letter equivalent.
	"""
	return text.replace('œ', 'oe').replace('æ', 'ae').replace('Æ', 'AE').replace('Œ', 'OE')

def _simplify(text):
	"""
	Removes accents and punctuation, expand ligatures, and converts to lowercase.
	"""
	text = _strip_diacritics(text)
	text = _remove_punctuation_and_spaces(text)
	text = _expand_ligatures(text)
	return text.casefold()

class SpellPlugin(PluginClass):
	plugin_info = {
		'name': 'Multilingual Spellchecker',
		'description': 'Spell-checks multilingual documents.',
		'author': 'Yi Xing'
	}

	plugin_notebook_properties = (
		('languages', 'string', 'Default languages (whitespace separated list of language codes)', ''),
	)

	@classmethod
	def check_dependencies(klass):
		return enchant_found, [('python3-enchant', enchant_found, True)]


class NoDictionaryError(Exception):
	pass


class SpellChecker:
	class _Mark:
		def __init__(self, buffer, name, start):
			self._buffer = buffer
			self._name = name
			self._mark = self._buffer.create_mark(self._name, start, True)

		@property
		def iter(self):
			return self._buffer.get_iter_at_mark(self._mark)

		@property
		def inside_word(self):
			return self.iter.inside_word()

		@property
		def word(self):
			start = self.iter
			if not start.starts_word():
				start.backward_word_start()
			end = self.iter
			if end.inside_word():
				end.forward_word_end()
			return start, end

		def move(self, location):
			self._buffer.move_mark(self._mark, location)
	
	class _Dict(enchant.Dict):
		def __init__(self, tag, broker):
			super().__init__(tag, broker)
			
			self._active = False
		
		@property
		def active(self):
			return self._active
		
		@active.setter
		def active(self, value):
			self._active = value
	
	PREFIX = 'gtkspellchecker'

	_re_numerals = re.compile(r'[0-9.,]+') # should only match beginning of word
	_re_cjk_ideographs = re.compile(r'[\u4E00-\u9FFF\u3400-\u4DBF\U00020000-\U0002A6DF\U0002A700-\U0002B73F\U0002B740-\U0002B81F\U0002B820-\U0002CEAF\U0002CEB0-\U0002EBEF\U0002F800-\U0002FA1F]') # should match anywhere in word
	_re_filter_line = re.compile('|'.join([
		r'(https?|ftp|file):((//)|(\\\\))+[\w\d:#@%/;$()~_?+-=\\.&]+',
		r'[\w\d]+@[\w\d.]+'
	]))

	def __init__(self, view, langs):
		self._view = view
		self._view.connect('populate-popup', lambda entry, menu: self._extend_menu(menu))
		self._view.connect('popup-menu', self._click_move_popup)
		self._view.connect('button-press-event', self._click_move_button)

		self._broker = enchant.Broker()

		self.supported_languages = self._broker.list_languages()
		if not self.supported_languages:
			raise NoDictionaryError()
		
		self._dictionaries = {
			language: self._Dict(language, self._broker)
			for language in self.supported_languages
		}
		if langs:
			for lang in langs:
				if lang in self._dictionaries:
					self._dictionaries[lang].active = True
		else:
			if 'en' in self._dictionaries:
				self._dictionaries['en'].active = True
			else:
				self._dictionaries[self.supported_languages[0]].active = True

		self._deferred_check = False
		self._enabled = True

		self.buffer_initialise()
	
	@property
	def enabled(self):
		return self._enabled

	@enabled.setter
	def enabled(self, enabled):
		if enabled and not self._enabled:
			self.enable()
		elif not enabled and self._enabled:
			self.disable()

	def buffer_initialise(self):
		self._misspelled = gtk.TextTag.new(f'{self.PREFIX}-misspelled')
		self._misspelled.set_property('underline', 4)

		self._buffer = self._view.get_buffer()
		self._buffer.connect('insert-text', self._before_text_insert)
		self._buffer.connect_after('insert-text', self._after_text_insert)
		self._buffer.connect_after('delete-range', self._range_delete)
		self._buffer.connect_after('mark-set', self._mark_set)

		start = self._buffer.get_bounds()[0]
		self._marks = {
			'insert-start': self._Mark(self._buffer, f'{self.PREFIX}-insert-start', start),
			'insert-end': self._Mark(self._buffer, f'{self.PREFIX}-insert-end', start),
			'click': self._Mark(self._buffer, f'{self.PREFIX}-click', start)
		}

		self._table = self._buffer.get_tag_table()
		self._table.add(self._misspelled)

		self.no_spell_check = self._table.lookup('no-spell-check')

		if not self.no_spell_check:
			self.no_spell_check = gtk.TextTag.new('no-spell-check')
			self._table.add(self.no_spell_check)
		
		self.recheck()
	
	def recheck(self):
		start, end = self._buffer.get_bounds()
		self.check_range(start, end, True)

	def disable(self):
		self._enabled = False
		start, end = self._buffer.get_bounds()
		self._buffer.remove_tag(self._misspelled, start, end)

	def enable(self):
		self._enabled = True
		self.recheck()
	
	def check_range(self, start, end, force_all=False):
		if not self._enabled:
			return
		
		if end.inside_word():
			end.forward_word_end()
		if not start.get_offset() and (start.inside_word() or start.ends_word()):
			start.backward_word_start()
		
		self._buffer.remove_tag(self._misspelled, start, end)
		cursor = self._buffer.get_iter_at_mark(self._buffer.get_insert())
		precursor = cursor.copy()
		precursor.backward_char()
		highlight = (cursor.has_tag(self._misspelled) or precursor.has_tag(self._misspelled))
		if not start.get_offset():
			start.forward_word_end()
			start.backward_word_start()
		word_start = start.copy()
		while word_start.compare(end) < 0:
			word_end = word_start.copy()
			word_end.forward_word_end()
			in_word = (word_start.compare(cursor) < 0 and cursor.compare(word_end) <= 0)
			if in_word and not force_all:
				if highlight:
					self._check_word(word_start, word_end)
				else:
					self._deferred_check = True
			else:
				self._check_word(word_start, word_end)
				self._deferred_check = False
			word_end.forward_word_end()
			word_end.backward_word_start()
			if word_start.equal(word_end):
				break
			word_start = word_end.copy()

	def _languages_menu(self):
		menu = gtk.Menu()
		group = []
		
		def toggle(item, language):
			self._dictionaries[language].active = item.get_active()
			self.recheck()

		for language in self.supported_languages:
			item = gtk.CheckMenuItem.new_with_label(language)
			group.append(item)
			item.set_active(self._dictionaries[language].active)
			item.connect('toggled', toggle, language)
			menu.append(item)
		
		return menu
	
	def _suggestions_menu(self, word):
		menu = gtk.Menu()
		
		def replace(item, suggestion):
			start, end = self._marks['click'].word
			offset = start.get_offset()
			self._buffer.begin_user_action()
			self._buffer.delete(start, end)
			self._buffer.insert(self._buffer.get_iter_at_offset(offset), suggestion)
			self._buffer.end_user_action()
		
		suggestions = set()
		for language in self.supported_languages:
			if self._dictionaries[language].active:
				suggestions.update(self._dictionaries[language].suggest(word))
		
		# Sort by simplified form
		suggestions = sorted(suggestions, key=_simplify)
		
		if suggestions:
			for suggestion in suggestions:
				item = gtk.MenuItem.new_with_label(suggestion)
				item.connect('activate', replace, suggestion)
				menu.append(item)
		else:
			item = gtk.MenuItem.new_with_label('No suggestions')
			item.set_sensitive(False)
			menu.append(item)
		
		return menu

	def _extend_menu(self, menu):
		if not self._enabled:
			return
		
		separator = gtk.SeparatorMenuItem.new()
		separator.show()
		menu.prepend(separator)

		languages = gtk.MenuItem.new_with_label('Languages')
		languages.set_submenu(self._languages_menu())
		languages.show_all()
		menu.prepend(languages)

		if self._marks['click'].inside_word:
			start, end = self._marks['click'].word
			if start.has_tag(self._misspelled):
				word = self._buffer.get_text(start, end, False)
				suggestions = gtk.MenuItem.new_with_label('Suggestions')
				suggestions.set_submenu(self._suggestions_menu(word))
				suggestions.show_all()
				menu.prepend(suggestions)

	def _click_move_popup(self, *args):
		self._marks['click'].move(self._buffer.get_iter_at_mark(self._buffer.get_insert()))
		return False
	
	def _click_move_button(self, widget, event):
		if event.button == 3:
			if self._deferred_check:
				self._check_deferred_range(True)
			x, y = self._view.window_to_buffer_coords(2, int(event.x), int(event.y))
			iter = self._view.get_iter_at_location(x, y)
			if isinstance(iter, tuple):
				iter = iter[1]
			self._marks['click'].move(iter)
		return False

	def _before_text_insert(self, textbuffer, location, text, length):
		self._marks['insert-start'].move(location)

	def _after_text_insert(self, textbuffer, location, text, length):
		start = self._marks['insert-start'].iter
		self.check_range(start, location)
		self._marks['insert-end'].move(location)

	def _range_delete(self, textbuffer, start, end):
		self.check_range(start, end)
	
	def _mark_set(self, textbuffer, location, mark):
		if mark == self._buffer.get_insert() and self._deferred_check:
			self._check_deferred_range(False)
	
	def _check_deferred_range(self, force_all):
		start = self._marks['insert-start'].iter
		end = self._marks['insert-end'].iter
		self.check_range(start, end, force_all)
	
	def _check_word(self, start, end):
		if start.has_tag(self.no_spell_check):
			return
		
		word = self._buffer.get_text(start, end, False).strip()
		if not word:
			return
		if self._re_numerals.match(word):
			return
		if self._re_cjk_ideographs.search(word):
			return
		
		line_start = self._buffer.get_iter_at_line(start.get_line())
		line_end = end.copy()
		line_end.forward_to_line_end()
		line = self._buffer.get_text(line_start, line_end, False)
		for match in self._re_filter_line.finditer(line):
			if match.start() <= start.get_offset() and match.end() >= end.get_offset():
				start = self._buffer.get_iter_at_line_offset(start.get_line(), match.start())
				end = self._buffer.get_iter_at_line_offset(start.get_line(), match.end())
				self._buffer.remove_tag(self._misspelled, start, end)
				return
		
		if all(not self._dictionaries[language].check(word)
			for language in self.supported_languages
			if self._dictionaries[language].active):
			self._buffer.apply_tag(self._misspelled, start, end)


class SpellPageViewExtension(PageViewExtension):
	class _Adapter(ConnectorMixin):
		def __init__(self, textview, languages):
			self._textview = textview
			self._languages = languages
			self._textbuffer = None
			self._checker = None
			self._active = False

			self.enable()
		
		def check_buffer_initialised(self):
			if self._checker and not self._check_tag_table():
				self._checker.buffer_initialise()

		def enable(self):
			if self._checker:
				self._checker.enable()
			else:
				self._clean_tag_table()
				self._checker = SpellChecker(self._textview, self._languages.split())

			self._textbuffer = self._textview.get_buffer()
			self.connectto_all(self._textbuffer, ('begin-insert-tree', 'end-insert-tree'))
			self._active = True

		def disable(self):
			if self._checker:
				self._checker.disable()
				self.disconnect_from(self._textbuffer)
				self._textbuffer = None

			self._active = False

		def detach(self):
			if self._checker:
				self.disable()
				self._clean_tag_table()
				self._checker = None

		def _check_tag_table(self):
			tags = []

			def filter_spell_tags(t):
				name = t.get_property('name')
				if name and name.startswith(SpellChecker.PREFIX):
					tags.append(t)

			table = self._textview.get_buffer().get_tag_table()
			table.foreach(filter_spell_tags)
			return tags

		def _clean_tag_table(self):
			## cleanup tag table - else next loading will fail
			table = self._textview.get_buffer().get_tag_table()
			for tag in self._check_tag_table():
				table.remove(tag)
		
		def on_begin_insert_tree(self, o, *a):
			self._checker.disable()

		def on_end_insert_tree(self, o, *a):
			self._checker.enable()

	def __init__(self, plugin, pageview):
		super().__init__(plugin, pageview)

		properties = self.plugin.notebook_properties(self.pageview.notebook)
		self._languages = properties['languages']
		self.connectto(properties, 'changed', self.on_properties_changed)

		self.uistate.setdefault('active', False)
		self.toggle_spellcheck(self.uistate['active'])
		self.connectto(self.pageview, 'page-changed', order=SIGNAL_AFTER)
	
	def on_properties_changed(self, properties):
		self._languages = properties['languages']
		textview = self.pageview.textview
		checker = getattr(textview, '_gtkspell', None)
		if checker:
			self.setup()

	@toggle_action(_('Check _spelling'), accelerator='F7') # T: menu item
	def toggle_spellcheck(self, active):
		textview = self.pageview.textview
		checker = getattr(textview, '_gtkspell', None)

		if active:
			if checker:
				checker.enable()
			else:
				self.setup()
		elif not active:
			if checker:
				checker.disable()
			# else pass

		self.uistate['active'] = active
	
	def on_page_changed(self, pageview, page):
		textview = pageview.textview
		checker = getattr(textview, '_gtkspell', None)
		if checker:
			# A new buffer may be initialized, but it could also be an existing buffer linked to page
			checker.check_buffer_initialised()
	
	def setup(self):
		textview = self.pageview.textview
		try:
			checker = self._Adapter(textview, self._languages)
		except Exception as e:
			ErrorDialog(self.pageview, (
				# _('Could not load spell checking'),
				f'{e.__class__.__name__}: {e}',
					# T: error message
				_('This could mean you don\'t have the proper\ndictionaries installed')
					# T: error message explanation
			)).run()
		else:
			textview._gtkspell = checker

	def teardown(self):
		textview = self.pageview.textview
		if hasattr(textview, '_gtkspell') and textview._gtkspell is not None:
			textview._gtkspell.detach()
			textview._gtkspell = None
