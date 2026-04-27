CXX ?= afl-clang-fast++
CXXFLAGS ?= -O2

target: target.cc
	$(CXX) $(CXXFLAGS) -o target target.cc

clean:
	rm -f target

.PHONY: clean
