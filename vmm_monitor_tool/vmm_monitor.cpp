// vmm_monitor — analog read-back of VMM3a internals through the monitor output
// and the ADC on each hybrid, using the measurement commands that exist in the
// installed p2basket-sc library (libp2b-sc.so) but have no command-line name.
//
// OURS. Lives in DAQ_Control_VMM_Beam/vmm_monitor_tool, is built in a folder
// of ours, and only *links* to /local/p2/deploy (read-only). Nothing of the
// slow-control developer's installation is modified.
//
// It loads the base YAML, applies exception files with the same positional
// grammar as TestBenchCERN/p2basket_sc_config.py, then runs measurements.
// IMPORTANT: every measurement REWRITES the VMM registers (it has to select
// the monitor source), from the model built here. Afterwards the chips hold
// "base + ext" with the monitor selection changed: run a normal
// "Configure chip" before taking data.
//
// Output: one JSON object per line on stdout (or --out FILE), e.g.
//   {"what":"chthr","channel":5,"sub":0,"fec":0,"tag":"","results":{"<alias>":[raw,signed],...}}
#include "run/P2bScCli.h"
#include "sc/cmd/MeasureVmmI2C.h"

#include <cstdint>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace p2b;

static std::vector<unsigned> parseList(const std::string& s)
{
	std::vector<unsigned> out;
	std::stringstream ss(s);
	std::string tok;
	while (std::getline(ss, tok, ','))
	{
		if (tok.empty()) continue;
		auto dash = tok.find('-');
		if (dash == std::string::npos) out.push_back(std::stoul(tok));
		else
		{
			unsigned a = std::stoul(tok.substr(0, dash)), b = std::stoul(tok.substr(dash + 1));
			for (unsigned i = a; i <= b; ++i) out.push_back(i);
		}
	}
	return out;
}

// Same grammar as p2basket_sc_config.py::ext_config, but strict: a malformed
// "Sys" line is an error here, never silently skipped.
static unsigned applyExt(sc::Backend& backend, const std::string& path)
{
	std::ifstream in(path);
	if (!in) throw std::runtime_error("cannot open ext file " + path);
	std::string line;
	unsigned n = 0, lineNo = 0;
	while (std::getline(in, line))
	{
		++lineNo;
		std::istringstream ls(line);
		std::vector<std::string> w;
		for (std::string t; ls >> t;) w.push_back(t);
		if (w.empty() || w[0] != "Sys") continue;
		auto bad = [&](const std::string& why) {
			throw std::runtime_error(path + ":" + std::to_string(lineNo) + ": " + why + ": " + line);
		};
		if (w.size() < 13 || w[2] != "Bec" || w[4] != "Hyb" || w[6] != "Vmm" || w[8] != "Reg" || w[10] != "Field")
			bad("not of the form 'Sys s Bec f Hyb h Vmm v Reg alias Field name value'");
		unsigned s = std::stoul(w[1]), f = std::stoul(w[3]), h = std::stoul(w[5]), v = std::stoul(w[7]);
		long val = std::stol(w[12]);
		auto& reg = backend.subsys(s).fec(f).hybrid(h).vmm(v).reg(w[9]);
		auto& field = reg.field(w[11]);
		field.setValue(static_cast<unsigned>(val));
		if (static_cast<long>(field.getValue()) != val) bad("value does not fit the field");
		++n;
	}
	return n;
}

static void emit(std::ostream& os, const std::string& what, long channel, unsigned sub, unsigned fec,
				 const sc::cmd::Command::ExecResults& res, const std::string& tag)
{
	os << "{\"what\":\"" << what << "\",\"channel\":" << channel << ",\"sub\":" << sub << ",\"fec\":" << fec
	   << ",\"tag\":\"" << tag << "\",\"results\":{";
	bool first = true;
	for (const auto& [name, value] : res)
	{
		if (!first) os << ",";
		first = false;
		// raw 32-bit word and its signed reading; which one is meaningful depends on the quantity
		os << "\"" << name << "\":[" << static_cast<uint32_t>(value) << "," << static_cast<int32_t>(value) << "]";
	}
	os << "}}" << std::endl;
}

int main(int argc, char** argv)
{
	std::string base, outPath, tag;
	std::vector<std::string> exts, whats;
	std::vector<unsigned> channels{0};
	bool dryRun = false, logDatagrams = false, ping = false;
	unsigned repeat = 1, adcChannel = 2;

	try
	{
		for (int i = 1; i < argc; ++i)
		{
			std::string a = argv[i];
			auto next = [&]() -> std::string {
				if (i + 1 >= argc) throw std::runtime_error("missing value after " + a);
				return argv[++i];
			};
			if (a == "--base") base = next();
			else if (a == "--ext") exts.push_back(next());
			else if (a == "--measure") whats.push_back(next());
			else if (a == "--channels") channels = parseList(next());
			else if (a == "--repeat") repeat = std::stoul(next());
			else if (a == "--adc-channel") adcChannel = std::stoul(next());
			else if (a == "--out") outPath = next();
			else if (a == "--tag") tag = next();
			else if (a == "--dry-run") dryRun = true;
			else if (a == "--log-datagrams") logDatagrams = true;
			else if (a == "--ping") ping = true;
			else
			{
				std::cerr << "usage: vmm_monitor --base BASE.yaml [--ext EXT.txt]... --measure WHAT [--measure WHAT]...\n"
							 "         [--channels 0,3,10-20] [--repeat N] [--tag TEXT] [--out FILE] [--dry-run] [--log-datagrams]\n"
							 "  WHAT: chthr   channel threshold minus channel baseline (per channel)\n"
							 "        chan    channel analog level (per channel)\n"
							 "        thrdac  threshold DAC   pulser  pulser DAC   bandgap   temp   adc (raw)\n"
							 "  --ext may be repeated; files are applied in order, later lines win.\n";
				return a == "--help" ? 0 : 2;
			}
		}
		if (base.empty() || whats.empty()) { std::cerr << "need --base and at least one --measure\n"; return 2; }

		run::P2bScCli::configLogger("ConsolePrompt");
		run::P2bScCli cli;
		cli.dryRun(dryRun);
		cli.logDatagrams(logDatagrams);
		cli.pingTest(ping);
		cli.cmdExec(cli.configQuery("config-file"), base);
		auto& backend = cli.scBackend();
		for (const auto& e : exts)
			std::cerr << "--- vmm_monitor: " << applyExt(backend, e) << " settings from " << e << std::endl;

		std::ofstream outFile;
		if (!outPath.empty()) outFile.open(outPath, std::ios::app);
		std::ostream& os = outPath.empty() ? std::cout : outFile;

		auto& connector = cli.scConnector(logDatagrams, ping, false);
		for (const auto& [subId, fecsMap] : connector.activeSubsMap())
		{
			for (const auto& [fecId, activeFec] : fecsMap)
			{
				auto& subHub = *activeFec.first;
				auto& fecConnect = const_cast<hw::Fec::Connect&>(activeFec.second);
				auto runOne = [&](sc::cmd::Udp::Command& cmd, const std::string& what, long ch) {
					cmd.logDatagram(logDatagrams);
					for (unsigned r = 0; r < repeat; ++r)
						emit(os, what, ch, subId, fecId, cmd.run(dryRun), tag);
				};
				for (const auto& what : whats)
				{
					if (what == "chthr")
						for (unsigned ch : channels) { sc::cmd::MeasureChannelThreshold c(fecConnect, subHub, ch); runOne(c, what, ch); }
					else if (what == "chan")
						for (unsigned ch : channels) { sc::cmd::MeasureVmmChannel c(fecConnect, subHub, ch); runOne(c, what, ch); }
					else if (what == "thrdac") { sc::cmd::MeasureThresholdDac c(fecConnect, subHub); runOne(c, what, -1); }
					else if (what == "pulser") { sc::cmd::MeasurePulserDac c(fecConnect, subHub); runOne(c, what, -1); }
					else if (what == "bandgap") { sc::cmd::MeasureBandgapReference c(fecConnect, subHub); runOne(c, what, -1); }
					else if (what == "temp") { sc::cmd::MeasureTemperature c(fecConnect, subHub); runOne(c, what, -1); }
					else if (what == "adc") { sc::cmd::ReadVmmAdc c(fecConnect, subHub, adcChannel); runOne(c, what, -1); }
					else throw std::runtime_error("unknown --measure " + what);
				}
			}
		}
	}
	catch (const std::exception& e) { std::cerr << "vmm_monitor: ERROR: " << e.what() << std::endl; return 1; }
	catch (const std::string& e) { std::cerr << "vmm_monitor: ERROR: " << e << std::endl; return 1; }
	catch (const char* e) { std::cerr << "vmm_monitor: ERROR: " << e << std::endl; return 1; }
	return 0;
}
