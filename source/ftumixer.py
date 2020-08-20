#!/usr/bin/env python3

# Copyright 2013-2020 Jonas Schulte-Coerne
# Copyright 2020 Grant Diffey
# Copyright 2020 Asbjørn Sæbø
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
try:	# for python3 compatibility
	import ConfigParser as configparser
except ImportError:
	import configparser
import functools
import re
import os
import select
import threading

import wx
import alsaaudio


class Mixer:
	"""
	This class is a wraps the interaction with ALSA.
	It is responsible for:
	  - setting the volume of an ALSA control
	  - asking for the volume of an ALSA control
	  - polling for changes of ALSA controls (does not work. Probably due to a
	    bug in pyalsaaudio)
	If a method of this class needs a channel as a parameter, it is given as an
	integer starting with 0. This differs from the GUI and the names of the
	Fast Track Ultra's ALSA controls, where channel numbers start with 1.
	"""

	def __init__(self, card_index, disable_effects, mute_most_digital_routes):
		"""
		@param card_index: the card index of the Fast Track Ultra that shall be
		                   controlled. (hw:1 means that the card index is 1)
		@param disable_effects: if True, all controls for the effects of the
		                        Fast Track Ultra will be muted
		@param mute_most_digital_routes: if True, all digital routes are muted,
		                                 except for routes from a digital input
		                                 to an output with the same number. This
		                                 way the routing of the digital signals
		                                 can be done with JACK
		"""
		self.__card_index = card_index
		self.__observers = []	# a list of functions that are called when a mixer value changes
		# poll for mixer value changes
		self.__descriptors_to_routes = {}
		self.__poll = select.epoll()
		self.__polling_thread = threading.Thread(target=self.__PollForChanges)
		self.__polling_thread.daemon = True
		self.__polling_thread.start()
		# create mixer objects
		regex_analog = re.compile("AIn\d - Out\d")
		regex_digital = re.compile("DIn\d - Out\d")
		self.__analog_routes = []
		self.__digital_routes = []
		self.__fx_control_names = []
		for name in alsaaudio.mixers(self.__card_index):
			if regex_analog.match(name):
				self.__CreateRoute(name=name, digital=False)
			elif regex_digital.match(name):
				self.__CreateRoute(name=name, digital=True)
			else:
				self.__fx_control_names.append(name)
		if disable_effects:
			self.DisableEffects()
		if mute_most_digital_routes:
			self.MuteMostDigitalRoutes()

	def GetNumberOfChannels(self):
		"""
		Returns the number of channels of the audio interface.
		It is assumed that the audio interface has as many input channels as it
		has output channels.
		"""
		return len(self.__analog_routes)

	def GetVolume(self, output_channel, input_channel, digital=False):
		"""
		Returns the volume of the ALSA control that is specified by the parameters.
		The result is an integer between 0 and 100.
		The channel numbers for the input and output channels start with 0.
		"""
		if digital:
			return self.__digital_routes[output_channel][input_channel].getvolume()[0]
		else:
			return self.__analog_routes[output_channel][input_channel].getvolume(alsaaudio.PCM_CAPTURE)[0]

	def SetVolume(self, value, output_channel, input_channel, digital=False):
		"""
		Sets the volume of the ALSA control that is specified by the parameters.
		The given value shall be an integer between 0 and 100.
		The channel numbers for the input and output channels start with 0.
		"""
		if digital:
			self.__digital_routes[output_channel][input_channel].setvolume(value, 0)
		else:
			self.__analog_routes[output_channel][input_channel].setvolume(value, 0, alsaaudio.PCM_CAPTURE)

	def AddObserver(self, function):
		"""
		Adds an observer function that will be called when an ALSA control has
		been changed by an external program.
		This function has to accept the two arguments "changed_analog_routes" and
		"changed_digital_routes". These arguments are lists of tuples of integers
		(output, input), that specify the routes that have changed.
		The channel numbers for the input and output channels start with 0.
		"""
		self.__observers.append(function)

	def DisableEffects(self):
		"""
		This method mutes all ALSA controls that are related to the Fast Track
		Ultra's built in effects processor.
		"""
		for n in self.__fx_control_names:
			c = alsaaudio.Mixer(n, cardindex=self.__card_index)
			if c.volumecap() != []:
				c.setvolume(0, 0)

	def MuteMostDigitalRoutes(self):
		"""
		This method mutes all digital routes, except for routes from a digital
		input to an output with the same number ("DIn1 - Out1", "DIn2 - Out2"...).
		This way the routing of the digital signals can be done with JACK or alike.
		"""
		for o in range(len(self.__digital_routes)):
			for i in range(len(self.__digital_routes[o])):
				if o != i:
					self.__digital_routes[o][i].setvolume(0, 0)

	def GetConfigDict(self):
		"""
		Returns a dictionary with the values of all ALSA controls for the Fast
		Track Ultra, including the effects controls.
		This dictionary can then saved to a config file.
		"""
		result = {}
		result["Analog"] = {}
		for o in range(len(self.__analog_routes)):
			for i in range(len(self.__analog_routes[o])):
				result["Analog"]["ain%i_to_out%i" % (i + 1, o + 1)] = self.GetVolume(output_channel=o, input_channel=i, digital=False)
		result["Digital"] = {}
		for o in range(len(self.__digital_routes)):
			for i in range(len(self.__analog_routes[o])):
				result["Digital"]["din%i_to_out%i" % (i + 1, o + 1)] = self.GetVolume(output_channel=o, input_channel=i, digital=True)
		result["Effects"] = {}
		for n in self.__fx_control_names:
			mixer = alsaaudio.Mixer(n, cardindex=self.__card_index)
			cname = n.replace(" ", "_").lower()
			if mixer.getenum() == ():
				result["Effects"][cname] = mixer.getvolume()[0]
			else:
				result["Effects"][cname] = mixer.getenum()[0]
		return result

	def ParseConfigDict(self, configdict):
		"""
		Sets the values of ALSA controls according to the values in the given
		dictionary.
		Only the controls for which the dictionary contains a value are changed.
		"""
		changed_analog_routes = []
		changed_digital_routes = []
		if "Analog" in configdict:
			for key in configdict["Analog"]:
				i, o = [int(s) - 1 for s in key.split("ain")[1].split("_to_out")]
				self.SetVolume(value=int(configdict["Analog"][key]), output_channel=o, input_channel=i, digital=False)
				changed_analog_routes.append((o, i))
		if "Digital" in configdict:
			for key in configdict["Digital"]:
				i, o = [int(s) - 1 for s in key.split("din")[1].split("_to_out")]
				self.SetVolume(value=int(configdict["Digital"][key]), output_channel=o, input_channel=i, digital=True)
				changed_digital_routes.append((o, i))
		if "Effects" in configdict:
			for n in self.__fx_control_names:
				cname = n.replace(" ", "_").lower()
				if cname in configdict["Effects"]:
					mixer = alsaaudio.Mixer(n, cardindex=self.__card_index)
					if mixer.getenum() == ():
						mixer.setvolume(int(configdict["Effects"][cname]), 0)
					elif configdict["Effects"][cname] in mixer.getenum()[1]:
						# I have not found a way to do this with pyalsaaudio, yet
						import subprocess
						call = []
						call.append("amixer")
						call.append("-c%i" % self.__card_index)
						call.append("sset")
						call.append(str(n))
						call.append(str(configdict["Effects"][cname]))
						subprocess.check_output(call)
		for o in self.__observers:
			o(changed_analog_routes, changed_digital_routes)

	def __CreateRoute(self, name, digital):
		"""
		Used internally to setup the alsaaudio.Mixer objects and the select.poll
		object that polls for changes in the ALSA controls.
		"""
		out_index = int(name[10]) - 1
		in_index = int(name[3]) - 1
		list_of_routes = self.__analog_routes
		if digital:
			list_of_routes = self.__digital_routes
		# create data structure
		for i in range(len(list_of_routes), out_index + 1):
			list_of_routes.append([])
		for i in range(len(list_of_routes[out_index]), in_index + 1):
			list_of_routes[out_index].append(None)
		# create mixer
		route = alsaaudio.Mixer(name, cardindex=self.__card_index)
		list_of_routes[out_index][in_index] = route
		# enable poll for changes
		descriptor = route.polldescriptors()[0]
		self.__poll.register(*descriptor)
		self.__descriptors_to_routes[descriptor[0]] = (out_index, in_index, digital, descriptor[1], descriptor[0])

	def __PollForChanges(self):
		"""
		This method is run in a separate thread. It polls for changes in the
		ALSA controls, so this program can update itself, when an external program
		changes a control.
		"""
		while True:  # this is a daemon thread, that is killed automatically in the end
			changed_analog_routes = []
			changed_digital_routes = []
			for d in self.__poll.poll(700):
				if d[1] & select.POLLIN:
					route = self.__descriptors_to_routes[d[0]]
					if route[2]:
						changed_digital_routes.append(route[0:2])
					else:
						changed_analog_routes.append(route[0:2])
					os.read(d[0], 512)
			if changed_analog_routes != [] or changed_digital_routes != []:
				for o in self.__observers:
					o(changed_analog_routes, changed_digital_routes)


class Gui:
	"""
	This class sets up the GUI for the mixer.
	It is responsible for:
	  - initializing the GUI's window
	  - adding itself to the given Config object
	  - running the GUI's main loop
	For information about how to use the GUI, see the README file.
	"""

	def __init__(self, mixer, config):
		"""
		@param mixer: a Mixer object
		@param config a Config object
		"""
		self.__mixer = mixer
		self.__config = config
		self.__config.SetGui(self)
		self.__app = wx.App()
		self.__frame = wx.Frame(parent=None, title="Fast Track Ultra Mixer", size=(480, 320))
		self.__app.SetTopWindow(self.__frame)
		# menu
		menubar = wx.MenuBar()
		self.__frame.SetMenuBar(menubar)
		filemenu = wx.Menu()
		menubar.Append(filemenu, "File")
		loaditem = filemenu.Append(id=wx.ID_ANY, item="Load config")
		self.__frame.Bind(wx.EVT_MENU, self.__OnLoadConfig, loaditem)
		saveitem = filemenu.Append(id=wx.ID_ANY, item="Save config")
		self.__frame.Bind(wx.EVT_MENU, self.__OnSaveConfig, saveitem)
		helpmenu = wx.Menu()
		menubar.Append(helpmenu, "Help")
		infoitem = helpmenu.Append(id=wx.ID_ANY, item="Info")
		self.__frame.Bind(wx.EVT_MENU, self.__OnInfo, infoitem)
		# notebook
		mainsizer = wx.BoxSizer(wx.VERTICAL)
		self.__frame.SetSizer(mainsizer)
		notebook = wx.Notebook(parent=self.__frame)
		mainsizer.Add(notebook, 1, wx.EXPAND)
		# master slider
		masterpanel = wx.Panel(parent=notebook)
		notebook.AddPage(masterpanel, "Master")
		masterpanelsizer = wx.BoxSizer(wx.HORIZONTAL)
		masterpanel.SetSizer(masterpanelsizer)
		mastersizer = wx.BoxSizer(wx.VERTICAL)
		masterpanelsizer.Add(mastersizer, 1, wx.EXPAND)
		mlabel = wx.StaticText(parent=masterpanel, label="Master")
		mastersizer.Add(mlabel, 0, wx.ALIGN_CENTER_HORIZONTAL)
		self.__masterslider = wx.Slider(parent=masterpanel, style=wx.SL_VERTICAL | wx.SL_INVERSE)
		mastersizer.Add(self.__masterslider, 1, wx.EXPAND)
		self.__masterslider.SetMin(0)
		self.__masterslider.SetMax(100)
		mastervalue = 0
		self.__masterlabel = wx.StaticText(parent=masterpanel)
		mastersizer.Add(self.__masterlabel, 0, wx.ALIGN_CENTER_HORIZONTAL)
		self.__masterslider.Bind(wx.EVT_SLIDER, self.__OnMaster)
		# macros
		buttonbox = wx.StaticBox(parent=masterpanel, label="Macros")
		buttonsizer = wx.StaticBoxSizer(box=buttonbox, orient=wx.VERTICAL)
		masterpanelsizer.Add(buttonsizer, 1, wx.EXPAND)
		mute_hardware_routes = wx.Button(parent=masterpanel, label="Mute hardware routes")
		buttonsizer.Add(mute_hardware_routes, 0, wx.EXPAND)
		mute_hardware_routes.Bind(wx.EVT_BUTTON, self.MuteHardwareRoutes)
		pass_through_inputs = wx.Button(parent=masterpanel, label="Pass through inputs")
		buttonsizer.Add(pass_through_inputs, 0, wx.EXPAND)
		pass_through_inputs.Bind(wx.EVT_BUTTON, self.PassThroughInputs)
		disable_effects = wx.Button(parent=masterpanel, label="Disable effects")
		buttonsizer.Add(disable_effects, 0, wx.EXPAND)
		disable_effects.Bind(wx.EVT_BUTTON, self.__DisableEffects)
		mute_most_digital_routes = wx.Button(parent=masterpanel, label="Mute most digital routes")
		buttonsizer.Add(mute_most_digital_routes, 0, wx.EXPAND)
		mute_most_digital_routes.Bind(wx.EVT_BUTTON, self.__MuteMostDigitalRoutes)
		# hardware routing sections
		self.__hardwarerouting_sliders = []
		self.__links = []
		self.__linkchoices = []
		for o in range(self.__mixer.GetNumberOfChannels()):
			self.__hardwarerouting_sliders.append([])
			panel = wx.Panel(parent=notebook)
			notebook.AddPage(panel, "Out%i" % (o + 1))
			panelsizer = wx.BoxSizer(wx.VERTICAL)
			panel.SetSizer(panelsizer)
			psizer = wx.BoxSizer(wx.HORIZONTAL)
			panelsizer.Add(psizer, 1, wx.EXPAND)
			for i in range(self.__mixer.GetNumberOfChannels()):
				ssizer = wx.BoxSizer(wx.VERTICAL)
				psizer.Add(ssizer, 1, wx.EXPAND)
				clabel = wx.StaticText(parent=panel, label="AIn%i" % (i + 1))
				ssizer.Add(clabel, 0, wx.ALIGN_CENTER_HORIZONTAL)
				slider = wx.Slider(parent=panel, style=wx.SL_VERTICAL | wx.SL_INVERSE)
				ssizer.Add(slider, 1, wx.EXPAND)
				slider.SetMin(0)
				slider.SetMax(100)
				slider.SetValue(self.__mixer.GetVolume(output_channel=o, input_channel=i))
				vlabel = wx.StaticText(parent=panel)
				vlabel.SetLabel(str(self.__mixer.GetVolume(output_channel=o, input_channel=i)))
				ssizer.Add(vlabel, 0, wx.ALIGN_CENTER_HORIZONTAL)
				partial = functools.partial(self.__OnHardwareRouting, output_channel=o, input_channel=i)
				slider.Bind(wx.EVT_SLIDER, partial)
				self.__hardwarerouting_sliders[o].append((slider, vlabel))
			# linking of output channels
			self.__links.append(None)
			lsizer = wx.BoxSizer(wx.HORIZONTAL)
			panelsizer.Add(lsizer, 0, wx.EXPAND)
			linklabel = wx.StaticText(parent=panel, label="Link to")
			lsizer.Add(linklabel, 0, wx.ALIGN_CENTER_VERTICAL)
			linkchoices = ["Out%i" % (i + 1) for i in range(0, self.__mixer.GetNumberOfChannels()) if i != o]
			linkchoices.insert(0, "None")
			linkchoice = wx.Choice(parent=panel, choices=linkchoices)
			lsizer.Add(linkchoice)
			partial = functools.partial(self.__OnLink, output_channel=o, choice=linkchoice)
			linkchoice.Bind(wx.EVT_CHOICE, partial)
			self.__linkchoices.append(linkchoice)
		# calculating value for master slider
			mastervalue += self.__mixer.GetVolume(output_channel=o, input_channel=o, digital=True)
		mastervalue /= float(self.__mixer.GetNumberOfChannels())
		self.__masterslider.SetValue(int(round(mastervalue)))
		self.__masterlabel.SetLabel(str(self.__masterslider.GetValue()))
		self.__mixer.AddObserver(self.__OnMixerEvent)

	def MainLoop(self):
		"""
		Layouts the main window, shows it and runs the wx main loop.
		This method blocks until the window is closed.
		"""
		self.__frame.Layout()
		self.__frame.Show()
		self.__app.MainLoop()

	def GetConfigDict(self):
		"""
		Returns a dictionary with the configuration values for the GUI.
		The dictionary will contain information about which outputs are linked.
		The dictionary can be saved with the configuration.
		"""
		result = {}
		result["GUI"] = {}
		for i in range(len(self.__links)):
			if self.__links[i] is None:
				result["GUI"]["link%ito" % (i + 1)] = "0"
			else:
				result["GUI"]["link%ito" % (i + 1)] = str(self.__links[i] + 1)
		return result

	def ParseConfigDict(self, configdict):
		"""
		Parses a configuration dictionary and sets up the GUI accordingly.
		"""
		if "GUI" in configdict:
			for key in configdict["GUI"]:
				link = int(key.lstrip("link").rstrip("to")) - 1
				to = int(configdict["GUI"][key]) - 1
				if to < 0:
					self.__links[link] = None
					self.__linkchoices[link].SetStringSelection("None")
				else:
					self.__links[link] = to
					self.__linkchoices[link].SetStringSelection("Out%i" % (to + 1))

	def __OnMaster(self, event):
		"""
		This will be called when the "master"-slider is moved.
		It sets the values of the ALSA controls for the routes from a digital input
		to its respective output (with the same number as the input) to the value
		of the slider.
		"""
		for c in range(self.__mixer.GetNumberOfChannels()):
			self.__mixer.SetVolume(value=self.__masterslider.GetValue(), output_channel=c, input_channel=c, digital=True)
		self.__masterlabel.SetLabel(str(self.__masterslider.GetValue()))

	def __OnHardwareRouting(self, event, output_channel, input_channel):
		"""
		This will be called when one of the sliders for the routing of the analog
		signals is moved.
		"""
		slider, vlabel = self.__hardwarerouting_sliders[output_channel][input_channel]
		volume = slider.GetValue()
		self.__mixer.SetVolume(value=volume, output_channel=output_channel, input_channel=input_channel)
		vlabel.SetLabel(str(volume))
		linked_output = self.__links[output_channel]
		if linked_output is not None and event is not None:
			linked_slider = self.__hardwarerouting_sliders[linked_output][input_channel][0]
			if event.GetId() != linked_slider.GetId():
				linked_slider.SetValue(volume)
				self.__OnHardwareRouting(event=event, output_channel=linked_output, input_channel=input_channel)

	def __OnMixerEvent(self, changed_analog_routes, changed_digital_routes):
		"""
		This will be passed to the mixer as an observer, that is called when an
		external program changes an ALSA control for the Fast Track Ultra.
		This method can be called from a different thread, as all accesses to the
		GUI are encapsulated in a nested function that is called with wx.CallAfter
		"""

		def worker():
			for o, i in changed_analog_routes:
				volume = self.__mixer.GetVolume(output_channel=o, input_channel=i)
				slider, vlabel = self.__hardwarerouting_sliders[o][i]
				if volume != slider.GetValue():
#					print("A change in route from input %i to output %i" % (i, o))
					slider.SetValue(volume)
					vlabel.SetLabel(str(volume))
			for o, i in changed_digital_routes:
				if o == i:
					mastervolume = 0
					for c in range(self.__mixer.GetNumberOfChannels()):
						mastervolume += self.__mixer.GetVolume(output_channel=c, input_channel=c, digital=True)
					mastervolume /= float(self.__mixer.GetNumberOfChannels())
					self.__masterslider.SetValue(int(round(mastervolume)))
					self.__masterlabel.SetLabel(str(self.__masterslider.GetValue()))
					break

		wx.CallAfter(worker)

	def __OnLink(self, event, output_channel, choice):
		"""
		This method is called when one of the "link to"-dropdown selectors has
		changed.
		"""
		selection = choice.GetStringSelection()
		if selection == "None":
			self.__links[output_channel] = None
		else:
			self.__links[output_channel] = int(selection[-1]) - 1

	def __OnLoadConfig(self, event):
		"""
		This method is called when the menu's "Load config" item is clicked.
		It shows a file selector dialog and loads the config from the selected file.
		"""
		dialog = wx.FileDialog(parent=self.__frame, style=wx.FD_OPEN)
		if dialog.ShowModal() == wx.ID_OK:
			self.__config.Load(filename=dialog.GetPath())
		dialog.Destroy()

	def __OnSaveConfig(self, event):
		"""
		This method is called when the menu's "Save config" item is clicked.
		It shows a file selector dialog and saves the config to the selected file.
		"""
		dialog = wx.FileDialog(parent=self.__frame, style=wx.FD_SAVE)
		if dialog.ShowModal() == wx.ID_OK:
			self.__config.Save(filename=dialog.GetPath())
		dialog.Destroy()

	def __OnInfo(self, event):
		"""
		This method is called when the menu's "Info" item is clicked.
		It shows a message box that displays information about the license of this
		program and where to get help.
		"""
		text = []
		text.append("Fast Track Ultra Mixer")
		text.append("")
		text.append("(c) Copyright Jonas Schulte-Coerne, Grant Diffey, Asbjørn Sæbø")
		text.append("This program is licensed under the terms of the Apache License, version 2.")
		text.append("For more information about the license see: http://www.apache.org/licenses/LICENSE-2.0")
		text.append("")
		text.append("For help about how to use this program, see https://github.com/JonasSC/FTU-Mixer")
		wx.MessageBox("\n".join(text), "Info", wx.OK | wx.ICON_INFORMATION)

	def MuteHardwareRoutes(self, event=None):
		"""
		A method for a button in the "Macros" box in the "master" tab of the notebook.
		It mutes all routes for the analog inputs.
		"""
		for o in range(self.__mixer.GetNumberOfChannels()):
			for i in range(self.__mixer.GetNumberOfChannels()):
				self.__hardwarerouting_sliders[o][i][0].SetValue(0)
				self.__OnHardwareRouting(event=None, output_channel=o, input_channel=i)

	def PassThroughInputs(self, event=None):
		"""
		A method for a button in the "Macros" box in the "master" tab of the notebook.
		It turns all routes from analog inputs to outputs with the same number
		to full volume. Other routes are not changed.
		"""
		for c in range(self.__mixer.GetNumberOfChannels()):
			self.__hardwarerouting_sliders[c][c][0].SetValue(100)
			self.__OnHardwareRouting(event=None, output_channel=c, input_channel=c)

	def __DisableEffects(self, event):
		"""
		A method for a button in the "Macros" box in the "master" tab of the notebook.
		It mutes all ALSA controls that are related to the Fast Track Ultra's
		built in effects processor.
		"""
		self.__mixer.DisableEffects()

	def __MuteMostDigitalRoutes(self, event):
		"""
		A method for a button in the "Macros" box in the "master" tab of the notebook.
		This method mutes all digital routes, except for routes from a digital
		input to an output with the same number ("DIn1 - Out1", "DIn2 - Out2"...).
		This way the routing of the digital signals can be done with JACK or alike.
		"""
		self.__mixer.MuteMostDigitalRoutes()


class Config:
	"""
	This class wraps the config file handling.
	It is responsible for:
	  - gathering config dictionaries from the mixer and the GUI, joining them
	    and saving them to a config file
	  - loading a config file to a dictionary and passing that to the mixer and
	    the GUI
	"""

	def __init__(self, mixer):
		"""
		@param mixer: a Mixer instance
		"""
		self.__mixer = mixer
		self.__gui = None

	def Load(self, filename):
		"""
		Loads a config file to a dictionary and passes that to the mixer and the
		GUI objects.
		"""
		configdict = {}
		parser = configparser.ConfigParser()
		parser.read(filename)
		for s in parser.sections():
			configdict[s] = {}
			for o in parser.options(s):
				configdict[s][o] = parser.get(s, o)
		self.__mixer.ParseConfigDict(configdict)
		if self.__gui is not None:
			self.__gui.ParseConfigDict(configdict)

	def Save(self, filename):
		"""
		Retrieves the config dictionaries from the mixer and the GUI and saves
		them to a config file.
		"""
		# generate configdict
		configdict = self.__mixer.GetConfigDict()
		if self.__gui is not None:
			gui_configdict = self.__gui.GetConfigDict()
			for section in gui_configdict:
				configdict[section] = gui_configdict[section]
		# write it to a config file
		parser = configparser.ConfigParser()
		for s in configdict:
			parser.add_section(s)
			for v in configdict[s]:
				parser.set(s, v, str(configdict[s][v]))
		with open(filename, 'w') as configfile:
			parser.write(configfile)

	def SetGui(self, gui):
		"""
		Sets the GUI object.
		"""
		self.__gui = gui


if __name__ == "__main__":
	card_index = None
	i = 0
	for c in alsaaudio.cards():
		if c in ("Ultra", "F8R"):
			card_index = i
			print(f"using card {card_index}")
			# parse command line arguments
			parser = argparse.ArgumentParser(description="A little mixer for the M-Audio Fast Track Ultra audio interfaces.")
			parser.add_argument("-c, --card", dest="card_index", action="store", type=int, default=card_index, help="The card index of the interface that shall be controlled.")
			parser.add_argument("-l, --load-config", dest="config", action="store", default="", help="A configuration file that shall be loaded on startup.")
			parser.add_argument("-X, --no-gui", dest="show_gui", action="store_false", default=True, help="Do not show the mixer GUI.")
			parser.add_argument("-F, --dont-disable-fx", dest="disable_effects", action="store_false", default=True, help="Do not disable all effects on startup.")
			parser.add_argument("-M, --dont-mute-most-digital-outputs", dest="mute_most_digital_routes", action="store_false", default=True, help="Do not mute most digital outputs on startup. Without this all digital outputs will be muted except for 'DIn1 - Out1', 'Din2 - Out2'... so the routing of the digital signals can be done with JACK.")
			parser.add_argument("-m, --mute-hardware-routes", dest="mute_hardware_routes", action="store_true", default=False, help="Mute all hardware routes of the analog signals.")
			parser.add_argument("-p, --pass-through-inputs", dest="pass_through_inputs", action="store_true", default=False, help="Route all analog inputs to their respective outputs. This does not affect other routes.")
			args = parser.parse_args()
			# setup necessary objects
			mixer = Mixer(card_index=args.card_index, disable_effects=args.disable_effects, mute_most_digital_routes=args.mute_most_digital_routes)
			config = Config(mixer=mixer)
			if args.show_gui:
				gui = Gui(mixer=mixer, config=config)
			# configure objects according to the command line arguments
			if args.mute_hardware_routes:
				gui.MuteHardwareRoutes()
			if args.pass_through_inputs:
				gui.PassThroughInputs()
			configpath = os.path.normpath(os.path.abspath(os.path.expanduser(os.path.expandvars(args.config))))
			if os.path.exists(configpath):
				config.Load(filename=configpath)
			# run the GUI if necessary
			if args.show_gui:
				gui.MainLoop()
			break
		i += 1
	else:
		print("No M-Audio Fast Track Ultra or Ultra 8R found. Exiting...")

